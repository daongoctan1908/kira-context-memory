"""Fail-open tracing helpers and request-local trace outcome state."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import CarrierT
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.domain.models.telemetry_context import TelemetryContext
from app.infrastructure.observability.langfuse_attributes import searchable_trace_metadata
from app.infrastructure.observability.redaction import safe_log_value

RequestOutcome = Literal["success", "degraded", "cancelled", "error"]
_OUTCOME_PRIORITY: dict[RequestOutcome, int] = {
    "success": 0,
    "degraded": 1,
    "cancelled": 2,
    "error": 3,
}


@dataclass(slots=True)
class RequestTraceState:
    """Mutable state shared by the ASGI request and its streaming child tasks."""

    span: Span
    outcome: RequestOutcome = "success"

    def mark_outcome(self, outcome: RequestOutcome) -> None:
        if _OUTCOME_PRIORITY[outcome] > _OUTCOME_PRIORITY[self.outcome]:
            self.outcome = outcome


_request_trace_state: ContextVar[RequestTraceState | None] = ContextVar(
    "request_trace_state",
    default=None,
)
_MASKED_OBSERVATION_STRINGS = {
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.observation.usage_details",
}
_CONTENT_CAPTURE_ATTRIBUTE = "kira.observation.content_capture_enabled"
_content_capture: ContextVar[bool] = ContextVar("telemetry_content_capture", default=False)


@contextmanager
def bind_content_capture(enabled: bool) -> Iterator[None]:
    """Scope explicit capture for standalone instrumentation; runtime policy takes precedence."""
    token = _content_capture.set(enabled)
    try:
        yield
    finally:
        _content_capture.reset(token)


def content_capture_enabled(span: Span) -> bool:
    """Allow content only on recording spans with an explicit opt-in policy."""
    try:
        if not span.is_recording():
            return False
        attributes = getattr(span, "attributes", None)
        if isinstance(attributes, Mapping) and _CONTENT_CAPTURE_ATTRIBUTE in attributes:
            return attributes[_CONTENT_CAPTURE_ATTRIBUTE] is True
        return _content_capture.get() is True
    except Exception:
        return False


def current_trace_fields() -> dict[str, str]:
    """Return IDs only for a valid, recording OTel span."""
    try:
        span = trace.get_current_span()
        if not span.is_recording():
            return {}
        context = span.get_span_context()
        if not context.is_valid:
            return {}
        return {
            "trace_id": format(context.trace_id, "032x"),
            "span_id": format(context.span_id, "016x"),
        }
    except Exception:
        return {}


def capture_telemetry_context(correlation_id: str) -> TelemetryContext | None:
    """Capture the active span plus an independent application correlation ID."""
    carrier: CarrierT = {}
    try:
        TraceContextTextMapPropagator().inject(carrier)
    except Exception:
        carrier = {}
    try:
        return TelemetryContext(
            correlation_id=correlation_id,
            traceparent=carrier.get("traceparent"),
            tracestate=carrier.get("tracestate"),
        )
    except (TypeError, ValueError):
        return None


def telemetry_context_links(value: TelemetryContext | None) -> tuple[trace.Link, ...]:
    """Build at most one producer link; malformed context degrades to no link."""
    if value is None or value.traceparent is None:
        return ()
    carrier: CarrierT = {"traceparent": value.traceparent}
    if value.tracestate is not None:
        carrier["tracestate"] = value.tracestate
    try:
        extracted = TraceContextTextMapPropagator().extract(carrier)
        span_context = trace.get_current_span(extracted).get_span_context()
        if not span_context.is_valid:
            return ()
        return (trace.Link(span_context),)
    except Exception:
        return ()


def telemetry_origin_trace_id(value: TelemetryContext | None) -> str | None:
    """Return the validated producer trace ID without trusting an arbitrary carrier."""
    if value is None or value.traceparent is None:
        return None
    return value.traceparent.split("-", 3)[1]


class LangfuseMetadataSpanProcessor(SpanProcessor):
    """Copy request-local IDs to filterable metadata without using network baggage."""

    def __init__(self, *, capture_content: bool = False) -> None:
        self._capture_content = capture_content

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        del parent_context
        try:
            # Local import avoids making the context facade depend on the runtime.
            from app.infrastructure.observability.context import current_context_fields

            span.set_attribute(_CONTENT_CAPTURE_ATTRIBUTE, self._capture_content)
            for key, value in current_context_fields().items():
                mapped = searchable_trace_metadata(key, value)
                if mapped is not None:
                    span.set_attribute(*mapped)
        except Exception:
            return

    def on_end(self, span: ReadableSpan) -> None:
        del span

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


@contextmanager
def start_span(
    tracer: Tracer,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, object] | None = None,
    links: Sequence[trace.Link] = (),
    context: Context | None = None,
) -> Iterator[Span]:
    """Start a span fail-open while preserving exceptions raised by the wrapped operation."""
    try:
        manager = tracer.start_as_current_span(
            name,
            context=context,
            kind=kind,
            links=links,
            record_exception=False,
            set_status_on_exception=False,
        )
        span = manager.__enter__()
    except Exception:
        yield trace.INVALID_SPAN
        return

    try:
        if attributes:
            for key, value in attributes.items():
                set_span_attribute(span, key, value)
        try:
            yield span
        except BaseException as error:
            record_span_error(span, error)
            try:
                manager.__exit__(type(error), error, error.__traceback__)
            except Exception:
                pass
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
    finally:
        # ``start_as_current_span`` owns span termination through ``__exit__``.
        # This block intentionally performs no business-affecting cleanup.
        pass


def set_span_attribute(span: Span, key: str, value: object) -> None:
    """Attach one bounded scalar attribute and ignore observer failures."""
    if key in {"langfuse.observation.input", "langfuse.observation.output"}:
        if not content_capture_enabled(span):
            return
    safe_key = safe_log_value(key)
    if (
        isinstance(safe_key, str)
        and safe_key in _MASKED_OBSERVATION_STRINGS
        and isinstance(value, str)
        and len(value) <= 8192
    ):
        safe_value: object = value
    else:
        safe_value = safe_log_value(value)
    if not isinstance(safe_key, str) or safe_value is None:
        return
    try:
        span.set_attribute(safe_key, safe_value)
    except Exception:
        return
    mapped = searchable_trace_metadata(safe_key, safe_value)
    if mapped is not None:
        try:
            span.set_attribute(*mapped)
        except Exception:
            return


def record_span_error(span: Span, error: BaseException) -> None:
    """Record only a safe error class, never exception messages or stack traces."""
    error_class = type(error).__name__
    set_span_attribute(span, "error.type", error_class)
    try:
        span.add_event("exception", attributes={"exception.type": error_class})
        span.set_status(Status(StatusCode.ERROR, error_class))
    except Exception:
        pass


@contextmanager
def bind_request_trace_state(state: RequestTraceState) -> Iterator[None]:
    """Bind one mutable request state without coupling callers to ASGI objects."""
    token = _request_trace_state.set(state)
    try:
        yield
    finally:
        _request_trace_state.reset(token)


def mark_request_outcome(outcome: RequestOutcome) -> None:
    """Raise the current request outcome severity when a trace is active."""
    state = _request_trace_state.get()
    if state is not None:
        state.mark_outcome(outcome)


def set_request_span_attribute(key: str, value: object) -> None:
    """Attach a safe value to the request root span when one exists."""
    state = _request_trace_state.get()
    if state is not None:
        set_span_attribute(state.span, key, value)


@contextmanager
def use_span_safely(span: Span) -> Iterator[None]:
    """Make an existing span current without allowing SDK failures into business flow."""
    try:
        manager = trace.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )
        manager.__enter__()
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass
