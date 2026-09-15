"""Dual-read Prometheus/OTel telemetry for the memory Worker migration."""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from time import perf_counter

from opentelemetry import trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobResult,
)
from app.domain.models.memory_job import MemoryJob, MemoryJobPurgeResult, MemoryJobStats
from app.infrastructure.observability.langfuse_attributes import (
    OBSERVATION_TYPE,
    masked_io_attributes,
    usage_attributes,
)
from app.infrastructure.observability.logging import configure_structured_logging
from app.infrastructure.observability.metrics import WorkerMetrics
from app.infrastructure.observability.tracing import set_span_attribute, start_span
from worker.runner import MemoryJobRunnerSnapshot

_QUEUE_STATUSES = ("pending", "processing", "completed", "dead")
_CLAIM_KINDS = ("new", "reclaimed")
_PROCESSING_OUTCOMES = ("success", "retry", "dead")
_CLEANUP_STATUSES = ("completed", "dead")
_STAGE_KINDS = {"internal": SpanKind.INTERNAL, "client": SpanKind.CLIENT}
_STAGE_TYPES = {
    "conversation.read_boundary": "span",
    "mem0.formation": "chain",
    "memory_job.transition": "span",
}


class _StageObservation:
    def __init__(self, span) -> None:
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
        self._set_attributes(masked_io_attributes(input_value=value))

    def set_output(self, value: object) -> None:
        self._set_attributes(masked_io_attributes(output_value=value))

    def set_usage(self, usage: Mapping[str, object]) -> None:
        self._set_attributes(usage_attributes(usage))

    def _set_attributes(self, attributes: Mapping[str, object]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)


def configure_worker_logging(level: str, *, deployment_environment: str = "development") -> None:
    """Install the allowlist-only logging boundary for Worker-owned loggers."""
    configure_structured_logging(
        "worker",
        level=level,
        service_name="kira-memory-worker",
        deployment_environment=deployment_environment,
    )


class MemoryJobTelemetry:
    """Phase 5 dual-read metrics, with OTel as the future retained instrumentation."""

    def __init__(self, *, tracer: Tracer | None = None, meter: Meter | None = None) -> None:
        self._tracer = tracer or trace.NoOpTracerProvider().get_tracer("worker.memory_jobs")
        self._otel = WorkerMetrics(meter)
        self._queue_snapshot_fresh = False
        self.registry = CollectorRegistry()
        self.queue_depth = Gauge(
            "kira_memory_job_queue_depth",
            "Current PostgreSQL memory-job rows by bounded status",
            ["status"],
            registry=self.registry,
        )
        self.oldest_pending_age = Gauge(
            "kira_memory_job_oldest_pending_age_seconds",
            "Age of the oldest pending memory job, or zero when none exist",
            registry=self.registry,
        )
        self.claims = Counter(
            "kira_memory_job_claim_total",
            "Successfully leased memory jobs",
            ["kind"],
            registry=self.registry,
        )
        self.processing = Counter(
            "kira_memory_job_processing_total",
            "Persisted memory-job processing outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.processing_latency = Histogram(
            "kira_memory_job_processing_duration_seconds",
            "Memory-job processing latency through its persisted transition",
            ["outcome"],
            buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
            registry=self.registry,
        )
        self.attempts = Histogram(
            "kira_memory_job_attempt_count",
            "Attempt number of each persisted processing outcome",
            buckets=(1, 2, 3, 4, 5, 8, 16, 32),
            registry=self.registry,
        )
        self.lifecycle_events = Histogram(
            "kira_memory_job_lifecycle_event_count",
            "Native Mem0 lifecycle events returned by successful jobs",
            buckets=(0, 1, 2, 3, 5, 10, 20),
            registry=self.registry,
        )
        self.cleanup = Counter(
            "kira_memory_job_cleanup_total",
            "Terminal memory jobs removed by retention cleanup",
            ["status"],
            registry=self.registry,
        )
        self.runner_active = Gauge(
            "kira_memory_worker_runner_active",
            "Whether the Worker runner task is active",
            registry=self.registry,
        )
        self.queue_database_available = Gauge(
            "kira_memory_job_queue_database_available",
            "Whether queue polling and the cached queue snapshot are available",
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "kira_memory_job_in_flight",
            "Currently leased jobs executing in this Worker process",
            registry=self.registry,
        )
        self.database_backoff = Gauge(
            "kira_memory_job_database_backoff_seconds",
            "Current runner database polling backoff",
            registry=self.registry,
        )

        for status in _QUEUE_STATUSES:
            self.queue_depth.labels(status).set(0)
        for kind in _CLAIM_KINDS:
            self.claims.labels(kind)
        for outcome in _PROCESSING_OUTCOMES:
            self.processing.labels(outcome)
            self.processing_latency.labels(outcome)
        for status in _CLEANUP_STATUSES:
            self.cleanup.labels(status)

    def configure_tracer(self, tracer: Tracer) -> None:
        """Attach the process tracer after the fail-open runtime has initialized."""
        self._tracer = tracer

    def configure_meter(self, meter: Meter) -> None:
        """Attach the process meter before the Worker starts processing jobs."""
        self._otel = WorkerMetrics(meter)

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: Mapping[str, object] | None = None,
    ) -> Iterator[_StageObservation]:
        span_attributes = dict(attributes or {})
        span_attributes[OBSERVATION_TYPE] = _STAGE_TYPES.get(name, "span")
        started = perf_counter()
        with start_span(
            self._tracer,
            name,
            kind=_STAGE_KINDS.get(kind, SpanKind.INTERNAL),
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

    def jobs_claimed(self, jobs: tuple[MemoryJob, ...]) -> None:
        new_count = sum(not job.reclaimed for job in jobs)
        reclaimed_count = len(jobs) - new_count
        if new_count:
            self.claims.labels("new").inc(new_count)
        if reclaimed_count:
            self.claims.labels("reclaimed").inc(reclaimed_count)
        self._otel.jobs_claimed(new_count=new_count, reclaimed_count=reclaimed_count)

    def job_processed(
        self,
        job: MemoryJob,
        result: ProcessMemoryJobResult,
        seconds: float,
    ) -> None:
        outcome = {
            MemoryJobProcessOutcome.COMPLETED: "success",
            MemoryJobProcessOutcome.RETRY: "retry",
            MemoryJobProcessOutcome.DEAD: "dead",
        }[result.outcome]
        self.processing.labels(outcome).inc()
        self.processing_latency.labels(outcome).observe(seconds)
        self.attempts.observe(job.attempt_count)
        if result.outcome is MemoryJobProcessOutcome.COMPLETED:
            self.lifecycle_events.observe(result.lifecycle_event_count)
        self._otel.job_processed(
            outcome="completed" if outcome == "success" else outcome,
            seconds=seconds,
            attempt_count=job.attempt_count,
            lifecycle_event_count=(
                result.lifecycle_event_count
                if result.outcome is MemoryJobProcessOutcome.COMPLETED
                else None
            ),
        )

    def queue_stats_observed(self, stats: MemoryJobStats) -> None:
        for status in _QUEUE_STATUSES:
            self.queue_depth.labels(status).set(getattr(stats, status))
        self.oldest_pending_age.set(stats.oldest_pending_age_seconds or 0.0)
        self._otel.queue_stats_observed(
            pending=stats.pending,
            processing=stats.processing,
            completed=stats.completed,
            dead=stats.dead,
            oldest_pending_age_seconds=stats.oldest_pending_age_seconds or 0.0,
        )

    def runner_observed(
        self,
        snapshot: MemoryJobRunnerSnapshot,
        *,
        queue_database_available: bool,
    ) -> None:
        self._queue_snapshot_fresh = queue_database_available
        self.runner_snapshot_observed(snapshot)

    def runner_snapshot_observed(self, snapshot: MemoryJobRunnerSnapshot) -> None:
        """Update gauges from process state; no dependency calls occur here."""
        self.runner_active.set(snapshot.running and not snapshot.stopping)
        queue_available = self._queue_snapshot_fresh and snapshot.database_available
        self.queue_database_available.set(queue_available)
        self.in_flight.set(snapshot.in_flight_count)
        self.database_backoff.set(snapshot.database_backoff_seconds)
        self._otel.runtime_observed(
            runner_active=snapshot.running and not snapshot.stopping,
            queue_database_available=queue_available,
            in_flight_count=snapshot.in_flight_count,
            database_backoff_seconds=snapshot.database_backoff_seconds,
        )

    def cleanup_observed(self, result: MemoryJobPurgeResult) -> None:
        """Observe rows removed by one successful bounded retention operation."""
        if result.completed:
            self.cleanup.labels("completed").inc(result.completed)
        if result.dead:
            self.cleanup.labels("dead").inc(result.dead)
        self._otel.cleanup_observed(completed=result.completed, dead=result.dead)

    def queue_wait_observed(self, seconds: float) -> None:
        """Observe time from job creation to this successful claim."""
        self._otel.queue_wait_observed(seconds)

    def stage_observed(self, stage: str, outcome: str, seconds: float) -> None:
        """Accept internal Mem0 stage durations without coupling Mem0 to OTel."""
        self._otel.stage_observed(stage, outcome, seconds)
