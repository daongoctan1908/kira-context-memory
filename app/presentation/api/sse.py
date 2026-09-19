"""Client-facing SSE serialization and stream lifecycle handling."""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.application.use_cases.handle_chat import ChatStreamSession
from app.domain.errors.kira import KiraClientError
from app.domain.models.conversation import AppendTurnResult, ConversationMessage
from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.tracing import (
    mark_request_outcome,
    set_request_span_attribute,
)
from app.presentation.api.errors import encode_gateway_error_event

logger = logging.getLogger(__name__)


async def stream_gateway_events(
    session: ChatStreamSession,
    correlation_id: str,
) -> AsyncIterator[bytes]:
    """Proxy KiRa frames incrementally and close downstream on every exit path."""
    try:
        async for event in session:
            yield f"data: {event.raw_data}\n\n".encode()
    except KiraClientError as error:
        mark_request_outcome("error")
        set_request_span_attribute("error.type", type(error).__name__)
        logger.warning(
            "KiRa stream failed",
            extra={
                "event": "kira.stream_failed",
                "correlation_id": correlation_id,
                "operation": "stream",
                "dependency": "kira",
                "error_class": type(error).__name__,
                "fallback_mode": "gateway_error",
            },
        )
        yield encode_gateway_error_event(error, correlation_id)
    except Exception as unexpected:
        mark_request_outcome("error")
        set_request_span_attribute("error.type", type(unexpected).__name__)
        logger.error(
            "Unexpected Gateway stream failure",
            extra={
                "event": "gateway.stream_failed",
                "correlation_id": correlation_id,
                "operation": "stream",
                "dependency": "kira",
                "error_class": type(unexpected).__name__,
                "fallback_mode": "gateway_error",
            },
        )
        error = KiraClientError("Unexpected Gateway stream failure")
        yield encode_gateway_error_event(error, correlation_id)
    finally:
        await session.aclose()
        logger.info(
            "KiRa stream closed",
            extra={
                "event": "kira.stream_closed",
                "correlation_id": correlation_id,
                "operation": "close_stream",
                "dependency": "kira",
            },
        )


class ChatStreamingResponse(StreamingResponse):
    """Close a suspended iterator even when ASGI send fails at a yielded frame."""

    def __init__(
        self,
        session: ChatStreamSession,
        correlation_id: str,
        *,
        turn_id: str | None = None,
        headers: dict[str, str],
    ) -> None:
        self._session = session
        self._correlation_id = correlation_id
        self._turn_id = turn_id
        self._events = stream_gateway_events(session, correlation_id)
        super().__init__(self._events, media_type="text/event-stream", headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with bind_observability_context(
            correlation_id=self._correlation_id,
            turn_id=self._turn_id,
        ):
            try:
                await super().__call__(scope, receive, send)
            finally:
                if not self._session.source_exhausted:
                    mark_request_outcome("cancelled")
                # Shield resource cleanup only, NEVER the completion/persistence callback.
                with anyio.CancelScope(shield=True):
                    try:
                        await self._events.aclose()
                    finally:
                        await self._session.aclose()


def _product_event(name: str, payload: dict[str, object]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n".encode()


async def stream_product_events(
    session: ChatStreamSession | None,
    *,
    correlation_id: str,
    turn_id: str,
    client_message_id: str,
    replayed_message: ConversationMessage | None,
    abort: Callable[[bool], Awaitable[None]],
) -> AsyncIterator[bytes]:
    """Expose stable product events and emit completion only after the DB commit."""
    base: dict[str, object] = {
        "turn_id": turn_id,
        "client_message_id": client_message_id,
    }
    yield _product_event("message.started", base)
    if replayed_message is not None:
        yield _product_event("message.delta", {**base, "text": replayed_message.content})
        logger.info(
            "Product chat replayed",
            extra={
                "event": "chat.replayed",
                "correlation_id": correlation_id,
                "turn_id": turn_id,
                "operation": "replay",
                "outcome": "success",
            },
        )
        yield _product_event("message.completed", {**base, "replayed": True})
        return
    if session is None:
        raise RuntimeError("live product stream requires a chat session")

    try:
        async for event in session:
            if event.text_fragment is not None:
                yield _product_event("message.delta", {**base, "text": event.text_fragment})
        completion = session.completion_result
        if not isinstance(completion, AppendTurnResult):
            raise RuntimeError("chat completion was not persisted")
        completed_payload = {**base, "replayed": False}
        if completion.memory_job_event_id is not None:
            completed_payload["event_id"] = str(completion.memory_job_event_id)
        logger.info(
            "Product chat completed",
            extra={
                "event": "chat.completed",
                "correlation_id": correlation_id,
                "turn_id": turn_id,
                "event_id": completed_payload.get("event_id"),
                "operation": "complete",
                "outcome": "success",
            },
        )
        yield _product_event("message.completed", completed_payload)
    except KiraClientError as error:
        await abort(False)
        mark_request_outcome("error")
        set_request_span_attribute("error.type", type(error).__name__)
        yield _product_event(
            "message.failed",
            {
                **base,
                "code": error.code,
                "message": str(error) or "KiRa request failed",
                "retryable": error.retryable,
                "correlation_id": correlation_id,
            },
        )
    except Exception as error:
        await abort(False)
        mark_request_outcome("error")
        set_request_span_attribute("error.type", type(error).__name__)
        logger.error(
            "Product chat stream failed",
            extra={
                "event": "chat.product_stream_failed",
                "correlation_id": correlation_id,
                "operation": "stream",
                "error_class": type(error).__name__,
                "fallback_mode": "message_failed",
            },
        )
        yield _product_event(
            "message.failed",
            {
                **base,
                "code": "CHAT_COMPLETION_FAILED",
                "message": "Chat request could not be completed",
                "retryable": True,
                "correlation_id": correlation_id,
            },
        )
    finally:
        await session.aclose()


class ProductChatStreamingResponse(StreamingResponse):
    """Product SSE lifecycle with fenced failure/cancellation transitions."""

    def __init__(
        self,
        *,
        correlation_id: str,
        turn_id: str,
        client_message_id: str,
        session: ChatStreamSession | None = None,
        replayed_message: ConversationMessage | None = None,
        on_abort: Callable[[bool], Awaitable[None]] | None = None,
        on_finish: Callable[[], Awaitable[None]] | None = None,
        headers: dict[str, str],
    ) -> None:
        self._session = session
        self._correlation_id = correlation_id
        self._turn_id = turn_id
        self._abort_callback = on_abort
        self._abort_started = False
        self._finish_callback = on_finish
        self._finish_started = False
        self._events = stream_product_events(
            session,
            correlation_id=correlation_id,
            turn_id=turn_id,
            client_message_id=client_message_id,
            replayed_message=replayed_message,
            abort=self._abort_once,
        )
        super().__init__(self._events, media_type="text/event-stream", headers=headers)

    async def _abort_once(self, cancelled: bool) -> None:
        if self._abort_started or self._abort_callback is None:
            return
        self._abort_started = True
        try:
            await self._abort_callback(cancelled)
        except Exception as error:
            logger.warning(
                "Chat request abandonment failed",
                extra={
                    "event": "chat.request_abandon_failed",
                    "correlation_id": self._correlation_id,
                    "operation": "abandon",
                    "error_class": type(error).__name__,
                    "fallback_mode": "lease_expiry",
                },
            )

    async def _finish_once(self) -> None:
        if self._finish_started or self._finish_callback is None:
            return
        self._finish_started = True
        try:
            await self._finish_callback()
        except Exception as error:
            logger.warning(
                "Chat traffic lease release failed",
                extra={
                    "event": "chat.traffic_release_failed",
                    "correlation_id": self._correlation_id,
                    "operation": "release",
                    "error_class": type(error).__name__,
                    "fallback_mode": "process_lifetime_cleanup",
                },
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with bind_observability_context(
            correlation_id=self._correlation_id,
            turn_id=self._turn_id,
        ):
            try:
                await super().__call__(scope, receive, send)
            finally:
                if (
                    self._session is not None
                    and not isinstance(self._session.completion_result, AppendTurnResult)
                    and not self._abort_started
                ):
                    mark_request_outcome("cancelled")
                    with anyio.CancelScope(shield=True):
                        await self._abort_once(True)
                with anyio.CancelScope(shield=True):
                    try:
                        await self._events.aclose()
                    finally:
                        if self._session is not None:
                            await self._session.aclose()
                with anyio.CancelScope(shield=True):
                    await self._finish_once()
