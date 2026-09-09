"""Process-local, low-cardinality Prometheus telemetry for the memory Worker."""

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobResult,
)
from app.domain.models.memory_job import MemoryJob, MemoryJobPurgeResult, MemoryJobStats
from app.infrastructure.observability.context import SafeJsonFormatter
from worker.runner import MemoryJobRunnerSnapshot

_QUEUE_STATUSES = ("pending", "processing", "completed", "dead")
_CLAIM_KINDS = ("new", "reclaimed")
_PROCESSING_OUTCOMES = ("success", "retry", "dead")
_CLEANUP_STATUSES = ("completed", "dead")


def configure_worker_logging(level: str) -> None:
    """Install the allowlist-only logging boundary for Worker-owned loggers."""
    worker_logger = logging.getLogger("worker")
    worker_logger.setLevel(level)
    if not any(getattr(handler, "kira_safe_handler", False) for handler in worker_logger.handlers):
        handler = logging.StreamHandler()
        handler.kira_safe_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(SafeJsonFormatter())
        worker_logger.addHandler(handler)
    worker_logger.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("mem0").setLevel(logging.CRITICAL)
    logging.getLogger("mem0").propagate = False


class MemoryJobTelemetry:
    """Metrics registry shared only within one Worker process."""

    def __init__(self) -> None:
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

    def jobs_claimed(self, jobs: tuple[MemoryJob, ...]) -> None:
        new_count = sum(not job.reclaimed for job in jobs)
        reclaimed_count = len(jobs) - new_count
        if new_count:
            self.claims.labels("new").inc(new_count)
        if reclaimed_count:
            self.claims.labels("reclaimed").inc(reclaimed_count)

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

    def queue_stats_observed(self, stats: MemoryJobStats) -> None:
        for status in _QUEUE_STATUSES:
            self.queue_depth.labels(status).set(getattr(stats, status))
        self.oldest_pending_age.set(stats.oldest_pending_age_seconds or 0.0)

    def runner_observed(
        self,
        snapshot: MemoryJobRunnerSnapshot,
        *,
        queue_database_available: bool,
    ) -> None:
        self.runner_active.set(snapshot.running and not snapshot.stopping)
        self.queue_database_available.set(queue_database_available and snapshot.database_available)
        self.in_flight.set(snapshot.in_flight_count)
        self.database_backoff.set(snapshot.database_backoff_seconds)

    def cleanup_observed(self, result: MemoryJobPurgeResult) -> None:
        """Observe rows removed by one successful bounded retention operation."""
        if result.completed:
            self.cleanup.labels("completed").inc(result.completed)
        if result.dead:
            self.cleanup.labels("dead").inc(result.dead)
