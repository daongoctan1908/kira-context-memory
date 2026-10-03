import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.domain.models.telemetry_context import TelemetryContext
from app.infrastructure.observability.context import ContextTelemetry
from app.infrastructure.observability.memory_observer import MemoryObserver
from app.infrastructure.observability.tracing import (
    LangfuseMetadataSpanProcessor,
    bind_content_capture,
    content_capture_enabled,
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


@pytest.mark.parametrize("capture", [False, True])
def test_process_policy_controls_content_for_gateway_and_worker_spans(capture: bool) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(LangfuseMetadataSpanProcessor(capture_content=capture))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.capture")

    with bind_content_capture(not capture):
        with ContextTelemetry(tracer=tracer).stage("rewrite.generate") as observation:
            observation.set_input({"password": "private-password"})
            observation.set_output("user@example.com")
            observation.set_usage({"input": 3, "output": 1})
            with MemoryObserver(tracer).observe("mem0.extract") as extraction:
                extraction.set_input({"api_key": "private-key"})
                extraction.set_output("+84 912 345 678")
                extraction.set_usage({"input": 2, "output": 1})

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        attributes = span.attributes
        assert attributes is not None
        assert ("langfuse.observation.input" in attributes) is capture
        assert ("langfuse.observation.output" in attributes) is capture
        assert "langfuse.observation.usage_details" in attributes
        assert "private-password" not in repr(attributes)
        assert "private-key" not in repr(attributes)
        assert "user@example.com" not in repr(attributes)
        assert "+84 912 345 678" not in repr(attributes)
    provider.shutdown()


def test_capture_defaults_off_and_nested_scopes_restore_policy() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test.capture")
    with tracer.start_as_current_span("root") as span:
        assert content_capture_enabled(span) is False
        with bind_content_capture(True):
            assert content_capture_enabled(span) is True
            with bind_content_capture(False):
                assert content_capture_enabled(span) is False
            assert content_capture_enabled(span) is True
        assert content_capture_enabled(span) is False
    with bind_content_capture(True):
        assert content_capture_enabled(trace.INVALID_SPAN) is False
    provider.shutdown()


def test_content_is_not_serialized_when_capture_is_off() -> None:
    class NeverSerialize(dict):
        def items(self):
            raise AssertionError("disabled capture must not touch content")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.capture")
    with MemoryObserver(tracer).observe("mem0.extract") as observation:
        observation.set_input(NeverSerialize(secret="never-export"))
        observation.set_output(NeverSerialize(secret="never-export"))
    assert not any(
        key.startswith("kira.observation.input") or key.startswith("kira.observation.output")
        for key in exporter.get_finished_spans()[0].attributes
    )
    provider.shutdown()
