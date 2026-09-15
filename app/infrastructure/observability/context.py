"""Application correlation context and the legacy Phase 0 Prometheus registry."""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from prometheus_client import CollectorRegistry, Counter, Histogram

from app.domain.models.telemetry_context import TelemetryContext
from app.domain.ports.context_observer import (
    ContextOperation,
    MemoryJobScheduleOutcome,
    MemorySearchOutcome,
    RewriteOutcome,
    StageKind,
    StageName,
    WriteOutcome,
)
from app.infrastructure.observability.langfuse_attributes import OBSERVATION_TYPE
from app.infrastructure.observability.tracing import (
    capture_telemetry_context,
    mark_request_outcome,
    set_request_span_attribute,
    set_span_attribute,
    start_span,
)

logger = logging.getLogger(__name__)

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
_event_id: ContextVar[str | None] = ContextVar("event_id", default=None)
_UNSET = object()

_SPAN_KINDS: dict[StageKind, SpanKind] = {
    "internal": SpanKind.INTERNAL,
    "client": SpanKind.CLIENT,
    "producer": SpanKind.PRODUCER,
}
_LANGFUSE_TYPES: dict[StageName, str] = {
    "identity.resolve": "span",
    "conversation.read_recent": "span",
    "memory.search": "retriever",
    "context.build": "chain",
    "rewrite.generate": "generation",
    "conversation.append_turn": "span",
    "memory_job.enqueue": "span",
}


def current_context_fields() -> dict[str, str]:
    """Return independent application identifiers bound to the current async context."""
    values = {
        "correlation_id": _correlation_id.get(),
        "turn_id": _turn_id.get(),
        "event_id": _event_id.get(),
    }
    return {key: value for key, value in values.items() if value is not None}


@contextmanager
def bind_observability_context(
    *,
    correlation_id: str | None | object = _UNSET,
    turn_id: str | None | object = _UNSET,
    event_id: str | None | object = _UNSET,
) -> Iterator[None]:
    """Bind app identifiers for one synchronous or asynchronous execution context."""
    tokens = []
    for variable, value in (
        (_correlation_id, correlation_id),
        (_turn_id, turn_id),
        (_event_id, event_id),
    ):
        if value is not _UNSET:
            tokens.append((variable, variable.set(value)))  # type: ignore[arg-type]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class _StageObservation:
    def __init__(self, span: Span) -> None:
        self._span = span

    def set_attribute(self, key: str, value: object) -> None:
        set_span_attribute(self._span, key, value)

    def set_outcome(self, outcome: str) -> None:
        self.set_attribute("kira.outcome", outcome)
        try:
            self._span.set_status(Status(StatusCode.ERROR if outcome == "error" else StatusCode.OK))
        except Exception:
            pass


class ContextTelemetry:
    def __init__(self, *, tracer: Tracer | None = None) -> None:
        self._tracer = tracer or trace.NoOpTracerProvider().get_tracer("app.application.context")
        self.registry = CollectorRegistry()
        self.recent_messages = Histogram(
            "kira_context_recent_messages",
            "Messages retained after context trimming",
            buckets=(0, 2, 4, 6, 8, 10, 20),
            registry=self.registry,
        )
        self.recent_tokens = Histogram(
            "kira_context_estimated_recent_tokens",
            "Estimated tokens, not exact Qwen tokens",
            buckets=(0, 100, 500, 1000, 2000, 3000, 6000),
            registry=self.registry,
        )
        self.memory_searches = Counter(
            "kira_memory_search_total",
            "Long-term-memory search outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.memory_search_latency = Histogram(
            "kira_memory_search_duration_seconds",
            "Attempted long-term-memory search latency",
            ["outcome"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 3, 5),
            registry=self.registry,
        )
        self.memory_search_results = Histogram(
            "kira_memory_search_results",
            "Ranked memories returned by successful searches",
            buckets=(0, 1, 2, 3, 5, 10),
            registry=self.registry,
        )
        self.memory_job_schedules = Counter(
            "kira_memory_job_schedule_total",
            "Gateway memory-job scheduling outcomes for eligible completed turns",
            ["outcome"],
            registry=self.registry,
        )
        self.rewrites = Counter(
            "kira_context_rewrite_total",
            "Rewrite outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.rewrite_latency = Histogram(
            "kira_context_rewrite_duration_seconds",
            "Attempted rewrite latency",
            ["outcome"],
            buckets=(0.05, 0.1, 0.5, 1, 2, 4, 8, 10),
            registry=self.registry,
        )
        self.degradations = Counter(
            "kira_context_degraded_total",
            "Contextual dependency failures",
            ["dependency", "operation"],
            registry=self.registry,
        )
        self.writes = Counter(
            "kira_conversation_write_total",
            "Completed turn write outcomes",
            ["outcome"],
            registry=self.registry,
        )

    def request_attribute(self, key: str, value: object) -> None:
        set_request_span_attribute(key, value)

    def capture_telemetry_context(self, correlation_id: str) -> TelemetryContext | None:
        """Capture the active enqueue span without exposing OTel to application code."""
        return capture_telemetry_context(correlation_id)

    @contextmanager
    def stage(
        self,
        name: StageName,
        *,
        kind: StageKind = "internal",
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[_StageObservation]:
        span_attributes = dict(attributes or {})
        span_attributes[OBSERVATION_TYPE] = _LANGFUSE_TYPES[name]
        with start_span(
            self._tracer,
            name,
            kind=_SPAN_KINDS[kind],
            attributes=span_attributes,
        ) as span:
            yield _StageObservation(span)

    def context_observed(self, message_count: int, estimated_tokens: int) -> None:
        self.recent_messages.observe(message_count)
        self.recent_tokens.observe(estimated_tokens)

    def memory_search_observed(
        self,
        outcome: MemorySearchOutcome,
        result_count: int | None,
        seconds: float | None,
    ) -> None:
        self.memory_searches.labels(outcome).inc()
        if seconds is not None:
            self.memory_search_latency.labels(outcome).observe(seconds)
        if result_count is not None:
            self.memory_search_results.observe(result_count)

    def rewrite_observed(self, outcome: RewriteOutcome, seconds: float | None) -> None:
        self.rewrites.labels(outcome).inc()
        if seconds is not None:
            self.rewrite_latency.labels(outcome).observe(seconds)

    def memory_job_schedule_observed(self, outcome: MemoryJobScheduleOutcome) -> None:
        self.memory_job_schedules.labels(outcome).inc()

    def degraded(
        self,
        correlation_id: str,
        operation: ContextOperation,
        error_class: str,
        fallback_mode: str,
    ) -> None:
        mark_request_outcome("degraded")
        set_request_span_attribute("kira.degraded.operation", operation)
        set_request_span_attribute("error.type", error_class)
        set_request_span_attribute("kira.fallback_mode", fallback_mode)
        dependency = {
            "identity": "identity",
            "memory_search": "mem0",
            "rewriter": "vllm",
        }.get(operation, "postgresql")
        self.degradations.labels(dependency, operation).inc()
        logger.warning(
            "Context capability degraded",
            extra={
                "event": "context.degraded",
                "correlation_id": correlation_id,
                "operation": operation,
                "dependency": dependency,
                "error_class": error_class,
                "fallback_mode": fallback_mode,
            },
        )

    def conversation_write_observed(self, outcome: WriteOutcome) -> None:
        self.writes.labels(outcome).inc()
