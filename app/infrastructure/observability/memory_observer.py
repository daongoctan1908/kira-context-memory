"""OpenTelemetry adapter for optional vendored-Mem0 observation hooks."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

from app.infrastructure.observability.langfuse_attributes import (
    OBSERVATION_MODEL,
    OBSERVATION_TYPE,
    masked_io_attributes,
    usage_attributes,
)
from app.infrastructure.observability.tracing import set_span_attribute, start_span

_SPAN_KINDS = {
    "internal": SpanKind.INTERNAL,
    "client": SpanKind.CLIENT,
}
_OBSERVATION_TYPES = {
    "mem0.existing_memory.search": "retriever",
    "mem0.extract": "generation",
    "mem0.extract.parse": "span",
    "mem0.memory.embed": "embedding",
    "mem0.deduplicate": "span",
    "mem0.persist": "span",
    "mem0.receipt": "span",
}


class _MemoryObservation:
    def __init__(self, span: Span) -> None:
        self._span = span
        self._usage: dict[str, int] = {}

    def set_attribute(self, key: str, value: object) -> None:
        set_span_attribute(self._span, key, value)
        if key == "gen_ai.request.model":
            set_span_attribute(self._span, OBSERVATION_MODEL, value)

    def set_outcome(self, outcome: str) -> None:
        self.set_attribute("kira.outcome", outcome)
        try:
            status = StatusCode.ERROR if outcome in {"error", "malformed"} else StatusCode.OK
            self._span.set_status(Status(status))
        except Exception:
            pass

    def set_input(self, value: object) -> None:
        self._set_attributes(masked_io_attributes(input_value=value))

    def set_output(self, value: object) -> None:
        self._set_attributes(masked_io_attributes(output_value=value))

    def set_usage(self, usage: Mapping[str, object]) -> None:
        for key in ("input", "output", "total"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self._usage[key] = self._usage.get(key, 0) + value
        self._set_attributes(usage_attributes(self._usage))

    def _set_attributes(self, attributes: Mapping[str, object]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)


class MemoryObserver:
    """Translate dependency-free Mem0 hooks into fail-open OTel spans."""

    def __init__(self, tracer: Tracer | None = None) -> None:
        self._tracer = tracer or trace.NoOpTracerProvider().get_tracer("app.infrastructure.memory")

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[_MemoryObservation]:
        span_attributes = dict(attributes or {})
        span_attributes[OBSERVATION_TYPE] = _OBSERVATION_TYPES.get(name, "span")
        with start_span(
            self._tracer,
            name,
            kind=_SPAN_KINDS.get(kind, SpanKind.INTERNAL),
            attributes=span_attributes,
        ) as span:
            yield _MemoryObservation(span)
