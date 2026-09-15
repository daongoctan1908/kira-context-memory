import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.domain.models.telemetry_context import TelemetryContext
from app.infrastructure.observability.tracing import (
    set_span_attribute,
    start_span,
    telemetry_origin_trace_id,
)


class FailingTracer:
    def start_as_current_span(self, *args, **kwargs):
        raise RuntimeError("observer failed")


def test_span_start_failure_degrades_to_invalid_span() -> None:
    with start_span(FailingTracer(), "test"):  # type: ignore[arg-type]
        assert trace.get_current_span() is trace.INVALID_SPAN


def test_business_exception_is_not_swallowed_by_tracing() -> None:
    tracer = TracerProvider().get_tracer("test")

    with pytest.raises(ValueError, match="business failure"):
        with start_span(tracer, "test"):
            raise ValueError("business failure")


def test_application_identifier_is_mirrored_to_searchable_langfuse_metadata() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with start_span(tracer, "test", attributes={"correlation_id": "abc-123"}) as span:
        set_span_attribute(span, "event_id", "event-123")
        set_span_attribute(span, "content", "not-metadata")

    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes is not None
    assert attributes["langfuse.trace.metadata.correlation_id"] == "abc-123"
    assert attributes["langfuse.trace.metadata.event_id"] == "event-123"
    assert "langfuse.trace.metadata.content" not in attributes


def test_origin_trace_id_is_read_only_from_validated_telemetry_context() -> None:
    origin = "4bf92f3577b34da6a3ce929d0e0e4736"
    carrier = TelemetryContext(
        correlation_id="0123456789abcdef0123456789abcdef",
        traceparent=f"00-{origin}-00f067aa0ba902b7-01",
    )

    assert telemetry_origin_trace_id(carrier) == origin
    assert telemetry_origin_trace_id(TelemetryContext(correlation_id="0" * 32)) is None
