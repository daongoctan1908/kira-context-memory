"""Fail-open tracing helpers that never swallow business exceptions."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

from app.infrastructure.observability.redaction import safe_log_value


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


@contextmanager
def start_span(
    tracer: Tracer,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, object] | None = None,
    links: Sequence[trace.Link] = (),
) -> Iterator[Span]:
    """Start a span fail-open while preserving exceptions raised by the wrapped operation."""
    try:
        manager = tracer.start_as_current_span(name, kind=kind, links=links)
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
            try:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            except Exception:
                pass
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
    safe_key = safe_log_value(key)
    safe_value = safe_log_value(value)
    if not isinstance(safe_key, str) or safe_value is None:
        return
    try:
        span.set_attribute(safe_key, safe_value)
    except Exception:
        return
