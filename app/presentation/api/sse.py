"""Client-facing SSE serialization and stream lifecycle handling."""

import logging
from collections.abc import AsyncIterator

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.application.use_cases.handle_chat import ChatStreamSession
from app.domain.errors.kira import KiraClientError
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
        logger.warning(
            "KiRa stream failed",
            extra={
                "correlation_id": correlation_id,
                "operation": "stream",
                "dependency": "kira",
                "error_class": type(error).__name__,
                "fallback_mode": "gateway_error",
            },
        )
        yield encode_gateway_error_event(error, correlation_id)
    except Exception as unexpected:
        logger.error(
            "Unexpected Gateway stream failure",
            extra={
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
        headers: dict[str, str],
    ) -> None:
        self._session = session
        self._events = stream_gateway_events(session, correlation_id)
        super().__init__(self._events, media_type="text/event-stream", headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Shield resource cleanup only, NEVER the completion/persistence callback.
            with anyio.CancelScope(shield=True):
                try:
                    await self._events.aclose()
                finally:
                    await self._session.aclose()
