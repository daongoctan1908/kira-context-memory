"""Application correlation, tracing, and dual-read Phase 5 metric facade."""

import asyncio
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from opentelemetry import trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from prometheus_client import CollectorRegistry, Counter, Histogram

from app.domain.models.telemetry_context import TelemetryContext
from app.domain.ports.context_observer import (
    ContextOperation,
    MemoryBranch,
    MemoryBranchOutcome,
    MemoryJobScheduleOutcome,
    MemorySearchOutcome,
    RewriteOutcome,
    StageKind,
    StageName,
    WriteOutcome,
)
from app.infrastructure.observability.langfuse_attributes import (
    OBSERVATION_TYPE,
    masked_io_attributes,
    usage_attributes,
)
from app.infrastructure.observability.metrics import GatewayMetrics
from app.infrastructure.observability.tracing import (
    capture_telemetry_context,
    content_capture_enabled,
    mark_request_outcome,
    set_request_span_attribute,
    set_span_attribute,
    start_span,
)

logger = logging.getLogger(__name__)

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)
_event_id: ContextVar[str | None] = ContextVar("event_id", default=None)
_origin_trace_id: ContextVar[str | None] = ContextVar("origin_trace_id", default=None)
_UNSET = object()

_SPAN_KINDS: dict[StageKind, SpanKind] = {
    "internal": SpanKind.INTERNAL,
    "client": SpanKind.CLIENT,
    "producer": SpanKind.PRODUCER,
}
_LANGFUSE_TYPES: dict[StageName, str] = {
    "identity.resolve": "span",
    "conversation.check_active": "span",
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
        "origin_trace_id": _origin_trace_id.get(),
    }
    return {key: value for key, value in values.items() if value is not None}


@contextmanager
def bind_observability_context(
    *,
    correlation_id: str | None | object = _UNSET,
    turn_id: str | None | object = _UNSET,
    event_id: str | None | object = _UNSET,
    origin_trace_id: str | None | object = _UNSET,
) -> Iterator[None]:
    """Bind app identifiers for one synchronous or asynchronous execution context."""
    tokens = []
    for variable, value in (
        (_correlation_id, correlation_id),
        (_turn_id, turn_id),
        (_event_id, event_id),
        (_origin_trace_id, origin_trace_id),
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
        self.outcome = "unknown"

    def set_attribute(self, key: str, value: object) -> None:
        set_span_attribute(self._span, key, value)

    def set_outcome(self, outcome: str) -> None:
        self.outcome = outcome
        self.set_attribute("kira.outcome", outcome)
        try:
            self._span.set_status(Status(StatusCode.ERROR if outcome == "error" else StatusCode.OK))
        except Exception:
            pass

    def set_input(self, value: object) -> None:
        if content_capture_enabled(self._span):
            self._set_attributes(masked_io_attributes(input_value=value))

    def set_output(self, value: object) -> None:
        if content_capture_enabled(self._span):
            self._set_attributes(masked_io_attributes(output_value=value))

    def set_usage(self, usage: Mapping[str, object]) -> None:
        self._set_attributes(usage_attributes(usage))

    def _set_attributes(self, attributes: Mapping[str, object]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)


class ContextTelemetry:
    def __init__(self, *, tracer: Tracer | None = None, meter: Meter | None = None) -> None:
        self._tracer = tracer or trace.NoOpTracerProvider().get_tracer("app.application.context")
        self._otel = GatewayMetrics(meter)
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
        self.memory_branches = Counter(
            "kira_memory_branch_total",
            "Scoped retrieval branch outcomes",
            ["branch", "outcome"],
            registry=self.registry,
        )
        self.memory_branch_latency = Histogram(
            "kira_memory_branch_duration_seconds",
            "Scoped retrieval branch latency",
            ["branch", "outcome"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 3, 5),
            registry=self.registry,
        )
        self.memory_branch_results = Histogram(
            "kira_memory_branch_results",
            "Memories returned by one scoped retrieval branch",
            ["branch"],
            buckets=(0, 1, 2, 3, 5, 10),
            registry=self.registry,
        )
        self.memory_scopes = Counter(
            "kira_memory_scope_total",
            "Formation scope classification outcomes per extracted candidate",
            ["scope", "origin"],
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
        started = perf_counter()
        with start_span(
            self._tracer,
            name,
            kind=_SPAN_KINDS[kind],
            attributes=span_attributes,
        ) as span:
            observation = _StageObservation(span)
            try:
                yield observation
            except BaseException as error:
                if observation.outcome == "unknown":
                    observation.set_outcome(
                        "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
                    )
                raise
            finally:
                self._otel.stage_observed(
                    name,
                    observation.outcome,
                    max(perf_counter() - started, 0.0),
                )

    def context_observed(self, message_count: int, estimated_tokens: int) -> None:
        self.recent_messages.observe(message_count)
        self.recent_tokens.observe(estimated_tokens)
        self._otel.context_observed(message_count, estimated_tokens)

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
        self._otel.memory_search_observed(outcome, result_count, seconds)

    def memory_branch_observed(
        self,
        branch: MemoryBranch,
        outcome: MemoryBranchOutcome,
        result_count: int,
        seconds: float,
    ) -> None:
        self.memory_branches.labels(branch, outcome).inc()
        self.memory_branch_latency.labels(branch, outcome).observe(max(seconds, 0.0))
        if outcome != "error":
            self.memory_branch_results.labels(branch).observe(result_count)
        self._otel.memory_branch_observed(branch, outcome, result_count, max(seconds, 0.0))

    def formation_scope_observed(
        self,
        *,
        conversation: int,
        global_count: int,
        fallback: int,
        invalid: int,
    ) -> None:
        if conversation:
            self.memory_scopes.labels("CONVERSATION", "valid").inc(conversation)
            self._otel.formation_scope_observed(
                scope="CONVERSATION", origin="valid", count=conversation
            )
        if global_count:
            self.memory_scopes.labels("GLOBAL", "valid").inc(global_count)
            self._otel.formation_scope_observed(scope="GLOBAL", origin="valid", count=global_count)
        if fallback:
            self.memory_scopes.labels("CONVERSATION", "fallback").inc(fallback)
            self._otel.formation_scope_observed(
                scope="CONVERSATION", origin="fallback", count=fallback
            )
        if invalid:
            self.memory_scopes.labels("unknown", "invalid").inc(invalid)
            self._otel.formation_scope_observed(scope="unknown", origin="invalid", count=invalid)

    def rewrite_observed(self, outcome: RewriteOutcome, seconds: float | None) -> None:
        self.rewrites.labels(outcome).inc()
        if seconds is not None:
            self.rewrite_latency.labels(outcome).observe(seconds)
        self._otel.rewrite_observed(outcome, seconds)

    def memory_job_schedule_observed(self, outcome: MemoryJobScheduleOutcome) -> None:
        self.memory_job_schedules.labels(outcome).inc()
        self._otel.memory_job_schedule_observed(outcome)

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
        self._otel.degraded(dependency, operation)
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
        self._otel.conversation_write_observed(outcome)

    def request_observed(self, outcome: str, seconds: float) -> None:
        """Record the complete `/chat` lifecycle, including SSE completion."""
        self._otel.request_observed(outcome, seconds)

    def kira_stream_observed(
        self,
        outcome: str,
        seconds: float,
        *,
        first_event_seconds: float | None,
        first_content_seconds: float | None,
    ) -> None:
        """Record KiRa stream completion and first-event/content latency."""
        self._otel.kira_stream_observed(
            outcome,
            seconds,
            first_event_seconds=first_event_seconds,
            first_content_seconds=first_content_seconds,
        )
