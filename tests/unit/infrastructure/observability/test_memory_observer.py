from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.infrastructure.observability.langfuse_attributes import (
    OBSERVATION_INPUT,
    OBSERVATION_MODEL,
    OBSERVATION_OUTPUT,
    OBSERVATION_USAGE,
)
from app.infrastructure.observability.memory_observer import MemoryObserver


class BrokenMapping(dict):
    def items(self):
        raise RuntimeError("private masking failure")


def test_memory_observer_emits_masked_generation_with_model_usage_and_outcome() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observer = MemoryObserver(provider.get_tracer("test"))

    with observer.observe("mem0.extract", kind="client") as observation:
        observation.set_input({"content": "subscriber user@example.com"})
        observation.set_attribute("gen_ai.request.model", "memory-model")
        observation.set_usage({"input": 10, "output": 4, "total": 14})
        observation.set_output({"memory": ["call +84 912 345 678"]})
        observation.set_outcome("success")

    span = exporter.get_finished_spans()[0]
    attributes = span.attributes
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes[OBSERVATION_MODEL] == "memory-model"
    assert attributes[OBSERVATION_USAGE] == '{"input":10,"output":4,"total":14}'
    assert "user@example.com" not in attributes[OBSERVATION_INPUT]
    assert "+84 912 345 678" not in attributes[OBSERVATION_OUTPUT]
    assert attributes["kira.outcome"] == "success"


def test_memory_observer_aggregates_multiple_provider_usage_reports() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with MemoryObserver(provider.get_tracer("test")).observe(
        "mem0.memory.embed", kind="client"
    ) as observation:
        observation.set_usage({"input": 2, "total": 2})
        observation.set_usage({"input": 3, "total": 3})
        observation.set_usage({"input": "bad", "output": -1})
        observation.set_outcome("success")

    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes[OBSERVATION_USAGE] == '{"input":5,"total":5}'


def test_memory_observer_omits_unsafe_model_and_unmaskable_content() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with MemoryObserver(provider.get_tracer("test")).observe(
        "mem0.extract", kind="client"
    ) as observation:
        observation.set_attribute("gen_ai.request.model", "unsafe model " + "secret" * 30)
        observation.set_input(BrokenMapping(secret="private-input"))
        observation.set_output(BrokenMapping(secret="private-output"))
        observation.set_usage({"input": "private", "output": -1})

    attributes = exporter.get_finished_spans()[0].attributes
    assert OBSERVATION_MODEL not in attributes
    assert OBSERVATION_INPUT not in attributes
    assert OBSERVATION_OUTPUT not in attributes
    assert OBSERVATION_USAGE not in attributes
    assert attributes["kira.observation.input.content_omitted"] == "masking_error"
    assert attributes["kira.observation.output.content_omitted"] == "masking_error"
    assert "private" not in repr(attributes)
