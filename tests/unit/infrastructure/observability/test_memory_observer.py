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


class ScopeMetricRecorder:
    def __init__(self):
        self.calls = []
        self.stages = []

    def formation_scope_observed(self, *, conversation, global_count, fallback, invalid):
        self.calls.append(
            {
                "conversation": conversation,
                "global_count": global_count,
                "fallback": fallback,
                "invalid": invalid,
            }
        )

    def stage_observed(self, stage, outcome, seconds):
        self.stages = getattr(self, "stages", [])
        self.stages.append((stage, outcome, seconds))


def test_memory_observer_forwards_scope_counts_to_metric_observer() -> None:
    recorder = ScopeMetricRecorder()
    observer = MemoryObserver(metric_observer=recorder)

    with observer.observe("mem0.extract.scope") as observation:
        observation.set_attribute("kira.memory.scope_conversation", 2)
        observation.set_attribute("kira.memory.scope_global", 1)
        observation.set_attribute("kira.memory.scope_fallback", 1)
        observation.set_attribute("kira.memory.scope_invalid", 1)
        observation.set_outcome("enforced")

    assert recorder.calls == [{"conversation": 2, "global_count": 1, "fallback": 1, "invalid": 1}]


def test_memory_observer_scope_forwarding_is_absent_without_metric_observer() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observer = MemoryObserver(provider.get_tracer("test"))

    with observer.observe("mem0.extract.scope") as observation:
        observation.set_attribute("kira.memory.scope_fallback", 3)
        observation.set_outcome("enforced")

    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes["kira.memory.scope_fallback"] == 3


def test_memory_observer_scope_forwarding_fail_open() -> None:
    class ExplodingRecorder(ScopeMetricRecorder):
        def formation_scope_observed(self, *, conversation, global_count, fallback, invalid):
            raise RuntimeError("metric boom")

    observer = MemoryObserver(metric_observer=ExplodingRecorder())

    with observer.observe("mem0.extract.scope") as observation:
        observation.set_attribute("kira.memory.scope_invalid", 2)
        observation.set_outcome("enforced")

    assert observation.outcome == "enforced"


def test_memory_observer_does_not_forward_scope_counts_for_other_stages() -> None:
    recorder = ScopeMetricRecorder()
    observer = MemoryObserver(metric_observer=recorder)

    with observer.observe("mem0.extract", kind="client") as observation:
        observation.set_attribute("kira.memory.scope_fallback", 5)
        observation.set_outcome("success")

    assert recorder.calls == []
