"""Pure ASGI tracing whose root span remains open through SSE completion."""

import asyncio
from time import perf_counter

import anyio
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.langfuse_attributes import OBSERVATION_TYPE
from app.infrastructure.observability.tracing import (
    RequestTraceState,
    bind_request_trace_state,
    current_trace_fields,
    record_span_error,
    set_span_attribute,
    start_span,
)


class ChatTracingMiddleware:
    """Trace one chat request without accepting public inbound trace context."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_chat_request(scope):
            await self._app(scope, receive, send)
            return

        correlation_id = scope.get("state", {}).get("correlation_id")
        tracer = _request_tracer(scope)
        started = perf_counter()
        normalized_path = (
            "/chat"
            if scope.get("path") == "/chat"
            else "/api/v1/conversations/{session_id}/messages"
        )
        attributes: dict[str, object] = {
            "http.request.method": "POST",
            "url.path": normalized_path,
            OBSERVATION_TYPE: "chain",
        }
        if isinstance(correlation_id, str):
            attributes["correlation_id"] = correlation_id

        # A fresh Context deliberately ignores untrusted public traceparent and baggage.
        with start_span(
            tracer,
            "chat.request",
            kind=SpanKind.SERVER,
            attributes=attributes,
            context=Context(),
        ) as span:
            state = RequestTraceState(span)
            status_code: int | None = None
            origin_trace_id = current_trace_fields().get("trace_id")
            if origin_trace_id is not None:
                set_span_attribute(span, "origin_trace_id", origin_trace_id)

            async def send_with_status(message: Message) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                await send(message)

            with bind_observability_context(
                correlation_id=correlation_id if isinstance(correlation_id, str) else None,
                origin_trace_id=origin_trace_id,
            ):
                with bind_request_trace_state(state):
                    try:
                        await self._app(scope, receive, send_with_status)
                    except BaseException as error:
                        if _is_cancellation(error):
                            state.mark_outcome("cancelled")
                        else:
                            state.mark_outcome("error")
                            _record_error(span, error)
                        raise
                    finally:
                        elapsed = max(perf_counter() - started, 0.0)
                        if status_code is not None:
                            set_span_attribute(span, "http.response.status_code", status_code)
                            if status_code >= 400:
                                state.mark_outcome("error")
                        set_span_attribute(span, "kira.outcome", state.outcome)
                        set_span_attribute(
                            span,
                            "kira.request.duration_seconds",
                            elapsed,
                        )
                        _observe_request_metric(scope, state.outcome, elapsed)
                        try:
                            if state.outcome == "error":
                                span.set_status(Status(StatusCode.ERROR))
                            elif state.outcome in {"success", "degraded"}:
                                span.set_status(Status(StatusCode.OK))
                        except Exception:
                            pass


def _request_tracer(scope: Scope) -> Tracer:
    try:
        return scope["app"].state.observability.get_tracer(
            "app.presentation.api",
            scope["app"].state.settings.app_version,
        )
    except Exception:
        return trace.NoOpTracerProvider().get_tracer("app.presentation.api")


def _observe_request_metric(scope: Scope, outcome: str, seconds: float) -> None:
    try:
        scope["app"].state.telemetry.request_observed(outcome, seconds)
    except Exception:
        pass


def _record_error(span, error: BaseException) -> None:
    record_span_error(span, error)


def _is_cancellation(error: BaseException) -> bool:
    cancellation_classes: tuple[type[BaseException], ...] = (
        asyncio.CancelledError,
        ClientDisconnect,
    )
    try:
        cancellation_classes += (anyio.get_cancelled_exc_class(),)
    except Exception:
        pass
    return isinstance(error, cancellation_classes)


def _is_chat_request(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") != "POST":
        return False
    path = scope.get("path")
    return path == "/chat" or (
        isinstance(path, str)
        and path.startswith("/api/v1/conversations/")
        and path.endswith("/messages")
    )
