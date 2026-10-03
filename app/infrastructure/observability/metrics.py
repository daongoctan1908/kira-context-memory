"""Fail-open OpenTelemetry metrics with a reviewed, bounded attribute contract."""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from opentelemetry import metrics
from opentelemetry.metrics import Meter, Observation

MetricKind = Literal["counter", "histogram", "observable_gauge"]


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Reviewed name, unit and aggregation contract for one application metric."""

    name: str
    kind: MetricKind
    unit: str = ""
    boundaries: tuple[float, ...] = ()


LEGACY_METRIC_MAP: Mapping[str, str] = {
    "kira_context_recent_messages": "kira.context.recent_messages",
    "kira_context_estimated_recent_tokens": "kira.context.estimated_recent_tokens",
    "kira_memory_search_total": "kira.memory.search.count",
    "kira_memory_search_duration_seconds": "kira.memory.search.duration",
    "kira_memory_search_results": "kira.memory.search.result_count",
    "kira_memory_job_schedule_total": "kira.memory.job.schedule.count",
    "kira_context_rewrite_total": "kira.context.rewrite.count",
    "kira_context_rewrite_duration_seconds": "kira.context.rewrite.duration",
    "kira_context_degraded_total": "kira.context.degraded.count",
    "kira_conversation_write_total": "kira.conversation.write.count",
    "kira_memory_job_queue_depth": "kira.memory.job.queue.depth",
    "kira_memory_job_oldest_pending_age_seconds": "kira.memory.job.oldest_pending.age",
    "kira_memory_job_claim_total": "kira.memory.job.claim.count",
    "kira_memory_job_processing_total": "kira.memory.job.process.count",
    "kira_memory_job_processing_duration_seconds": "kira.memory.job.process.duration",
    "kira_memory_job_attempt_count": "kira.memory.job.attempt.number",
    "kira_memory_job_lifecycle_event_count": "kira.memory.lifecycle_event.count",
    "kira_memory_job_cleanup_total": "kira.memory.job.cleanup.count",
    "kira_memory_worker_runner_active": "kira.memory.worker.runner.active",
    "kira_memory_job_queue_database_available": "kira.memory.job.queue.database.available",
    "kira_memory_job_in_flight": "kira.memory.job.in_flight",
    "kira_memory_job_database_backoff_seconds": "kira.memory.job.database_backoff",
}

_DURATION_BOUNDARIES = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)

METRIC_SPECS: Mapping[str, MetricSpec] = {
    "recent_messages": MetricSpec(
        "kira.context.recent_messages", "histogram", boundaries=(0, 2, 4, 6, 8, 10, 20)
    ),
    "recent_tokens": MetricSpec(
        "kira.context.estimated_recent_tokens",
        "histogram",
        boundaries=(0, 100, 500, 1000, 2000, 3000, 6000),
    ),
    "memory_search_count": MetricSpec("kira.memory.search.count", "counter", "{search}"),
    "memory_search_duration": MetricSpec(
        "kira.memory.search.duration",
        "histogram",
        "s",
        (0.01, 0.05, 0.1, 0.5, 1, 2, 3, 5),
    ),
    "memory_search_results": MetricSpec(
        "kira.memory.search.result_count", "histogram", boundaries=(0, 1, 2, 3, 5, 10)
    ),
    "memory_branch_count": MetricSpec("kira.memory.branch.count", "counter", "{search}"),
    "memory_branch_duration": MetricSpec(
        "kira.memory.branch.duration",
        "histogram",
        "s",
        (0.01, 0.05, 0.1, 0.5, 1, 2, 3, 5),
    ),
    "memory_branch_results": MetricSpec(
        "kira.memory.branch.result_count", "histogram", boundaries=(0, 1, 2, 3, 5, 10)
    ),
    "formation_scope": MetricSpec("kira.memory.scope.count", "counter", "{candidate}"),
    "memory_job_schedule": MetricSpec("kira.memory.job.schedule.count", "counter", "{schedule}"),
    "rewrite_count": MetricSpec("kira.context.rewrite.count", "counter", "{rewrite}"),
    "rewrite_duration": MetricSpec(
        "kira.context.rewrite.duration",
        "histogram",
        "s",
        (0.05, 0.1, 0.5, 1, 2, 4, 8, 10),
    ),
    "degraded_count": MetricSpec("kira.context.degraded.count", "counter", "{degradation}"),
    "conversation_write": MetricSpec("kira.conversation.write.count", "counter", "{write}"),
    "request_duration": MetricSpec(
        "kira.chat.request.duration", "histogram", "s", _DURATION_BOUNDARIES
    ),
    "stage_duration": MetricSpec("kira.stage.duration", "histogram", "s", _DURATION_BOUNDARIES),
    "kira_stream_duration": MetricSpec(
        "kira.stream.duration", "histogram", "s", _DURATION_BOUNDARIES
    ),
    "kira_first_event": MetricSpec(
        "kira.stream.first_event.duration", "histogram", "s", _DURATION_BOUNDARIES
    ),
    "kira_first_content": MetricSpec(
        "kira.stream.first_content.duration", "histogram", "s", _DURATION_BOUNDARIES
    ),
    "queue_depth": MetricSpec("kira.memory.job.queue.depth", "observable_gauge", "{job}"),
    "oldest_pending_age": MetricSpec("kira.memory.job.oldest_pending.age", "observable_gauge", "s"),
    "job_claim_count": MetricSpec("kira.memory.job.claim.count", "counter", "{job}"),
    "job_process_count": MetricSpec("kira.memory.job.process.count", "counter", "{job}"),
    "job_process_duration": MetricSpec(
        "kira.memory.job.process.duration",
        "histogram",
        "s",
        (0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
    ),
    "job_attempt": MetricSpec(
        "kira.memory.job.attempt.number", "histogram", "{attempt}", (1, 2, 3, 4, 5, 8, 16, 32)
    ),
    "lifecycle_event": MetricSpec(
        "kira.memory.lifecycle_event.count",
        "histogram",
        "{event}",
        (0, 1, 2, 3, 5, 10, 20),
    ),
    "cleanup_count": MetricSpec("kira.memory.job.cleanup.count", "counter", "{job}"),
    "runner_active": MetricSpec("kira.memory.worker.runner.active", "observable_gauge", "1"),
    "queue_database_available": MetricSpec(
        "kira.memory.job.queue.database.available", "observable_gauge", "1"
    ),
    "in_flight": MetricSpec("kira.memory.job.in_flight", "observable_gauge", "{job}"),
    "database_backoff": MetricSpec("kira.memory.job.database_backoff", "observable_gauge", "s"),
    "queue_wait": MetricSpec(
        "kira.memory.job.queue_wait.duration",
        "histogram",
        "s",
        (0.1, 1, 5, 15, 30, 60, 300, 900, 3600),
    ),
}

_ALLOWED_ATTRIBUTE_VALUES: Mapping[str, frozenset[str]] = {
    "stage": frozenset(
        {
            "identity.resolve",
            "conversation.check_active",
            "conversation.read_recent",
            "memory.search",
            "context.build",
            "rewrite.generate",
            "conversation.append_turn",
            "memory_job.enqueue",
            "conversation.read_boundary",
            "mem0.formation",
            "memory_job.transition",
            "mem0.existing_memory.search",
            "mem0.extract",
            "mem0.extract.parse",
            "mem0.extract.scope",
            "mem0.memory.embed",
            "mem0.deduplicate",
            "mem0.persist",
            "mem0.receipt",
        }
    ),
    "operation": frozenset(
        {"identity", "memory_search", "postgres_read", "postgres_write", "rewriter"}
    ),
    "outcome": frozenset(
        {
            "success",
            "error",
            "bypass",
            "scheduled",
            "disabled",
            "duplicate",
            "inserted",
            "degraded",
            "cancelled",
            "completed",
            "skipped",
            "retry",
            "dead",
            "transition_error",
            "malformed",
            "parsed",
            "empty_valid",
            "deduplicated_empty",
            "receipt_replay",
            "created",
            "conflict",
            "authenticated",
            "active",
            "inactive",
            "anonymous",
            "replay",
            "miss",
            "empty",
            "partial",
            "unknown",
            "enforced",
            "scope_dropped",
        }
    ),
    "dependency": frozenset({"identity", "mem0", "vllm", "postgresql", "kira", "otel"}),
    "status": frozenset({"pending", "processing", "completed", "dead"}),
    "kind": frozenset({"new", "reclaimed"}),
    "branch": frozenset({"conversation", "global"}),
    "scope": frozenset({"CONVERSATION", "GLOBAL", "unknown"}),
    "origin": frozenset({"valid", "fallback", "invalid"}),
}


def bounded_metric_attributes(attributes: Mapping[str, str]) -> dict[str, str]:
    """Validate metric attributes against fixed keys and enum values.

    Unknown keys are rejected instead of silently stripping them so identifiers can never be
    accidentally exported after a call-site typo or future refactor.
    """

    bounded: dict[str, str] = {}
    for key, value in attributes.items():
        allowed = _ALLOWED_ATTRIBUTE_VALUES.get(key)
        if allowed is None or value not in allowed:
            raise ValueError("metric attributes must use reviewed bounded enums")
        bounded[key] = value
    return bounded


class _NoOpInstrument:
    def add(self, amount: int | float, attributes: Mapping[str, str] | None = None) -> None:
        del amount, attributes

    def record(self, amount: int | float, attributes: Mapping[str, str] | None = None) -> None:
        del amount, attributes


def _create_instrument(meter: Meter, spec: MetricSpec) -> Any:
    kwargs: dict[str, object] = {"unit": spec.unit}
    if spec.kind == "counter":
        method = meter.create_counter
    elif spec.kind == "histogram":
        method = meter.create_histogram
        kwargs["explicit_bucket_boundaries_advisory"] = spec.boundaries
    else:
        raise ValueError("observable gauges require callbacks")
    try:
        return method(spec.name, **kwargs)
    except Exception:
        return _NoOpInstrument()


def _safe_add(instrument: Any, amount: int | float, **attributes: str) -> None:
    try:
        if isinstance(amount, bool) or not math.isfinite(float(amount)) or amount < 0:
            return
        instrument.add(amount, bounded_metric_attributes(attributes))
    except Exception:
        pass


def _safe_record(instrument: Any, amount: int | float, **attributes: str) -> None:
    try:
        if isinstance(amount, bool) or not math.isfinite(float(amount)) or amount < 0:
            return
        instrument.record(amount, bounded_metric_attributes(attributes))
    except Exception:
        pass


class GatewayMetrics:
    """OTel metric facade for Gateway operations; every method is fail-open."""

    def __init__(self, meter: Meter | None = None) -> None:
        resolved = meter or metrics.NoOpMeterProvider().get_meter("kira.gateway.metrics")
        self._recent_messages = _create_instrument(resolved, METRIC_SPECS["recent_messages"])
        self._recent_tokens = _create_instrument(resolved, METRIC_SPECS["recent_tokens"])
        self._search_count = _create_instrument(resolved, METRIC_SPECS["memory_search_count"])
        self._search_duration = _create_instrument(resolved, METRIC_SPECS["memory_search_duration"])
        self._search_results = _create_instrument(resolved, METRIC_SPECS["memory_search_results"])
        self._branch_count = _create_instrument(resolved, METRIC_SPECS["memory_branch_count"])
        self._branch_duration = _create_instrument(resolved, METRIC_SPECS["memory_branch_duration"])
        self._branch_results = _create_instrument(resolved, METRIC_SPECS["memory_branch_results"])
        self._formation_scope = _create_instrument(resolved, METRIC_SPECS["formation_scope"])
        self._schedule_count = _create_instrument(resolved, METRIC_SPECS["memory_job_schedule"])
        self._rewrite_count = _create_instrument(resolved, METRIC_SPECS["rewrite_count"])
        self._rewrite_duration = _create_instrument(resolved, METRIC_SPECS["rewrite_duration"])
        self._degraded_count = _create_instrument(resolved, METRIC_SPECS["degraded_count"])
        self._write_count = _create_instrument(resolved, METRIC_SPECS["conversation_write"])
        self._request_duration = _create_instrument(resolved, METRIC_SPECS["request_duration"])
        self._stage_duration = _create_instrument(resolved, METRIC_SPECS["stage_duration"])
        self._stream_duration = _create_instrument(resolved, METRIC_SPECS["kira_stream_duration"])
        self._first_event = _create_instrument(resolved, METRIC_SPECS["kira_first_event"])
        self._first_content = _create_instrument(resolved, METRIC_SPECS["kira_first_content"])
        self._formation_scope = _create_instrument(resolved, METRIC_SPECS["formation_scope"])

    def context_observed(self, message_count: int, estimated_tokens: int) -> None:
        _safe_record(self._recent_messages, message_count)
        _safe_record(self._recent_tokens, estimated_tokens)

    def memory_search_observed(
        self, outcome: str, result_count: int | None, seconds: float | None
    ) -> None:
        _safe_add(self._search_count, 1, outcome=outcome)
        if seconds is not None:
            _safe_record(self._search_duration, seconds, outcome=outcome)
        if result_count is not None:
            _safe_record(self._search_results, result_count)

    def memory_branch_observed(
        self, branch: str, outcome: str, result_count: int, seconds: float
    ) -> None:
        _safe_add(self._branch_count, 1, branch=branch, outcome=outcome)
        _safe_record(self._branch_duration, seconds, branch=branch, outcome=outcome)
        if outcome != "error":
            _safe_record(self._branch_results, result_count, branch=branch)

    def formation_scope_observed(self, *, scope: str, origin: str, count: int) -> None:
        if count > 0:
            _safe_add(self._formation_scope, count, scope=scope, origin=origin)

    def rewrite_observed(self, outcome: str, seconds: float | None) -> None:
        _safe_add(self._rewrite_count, 1, outcome=outcome)
        if seconds is not None:
            _safe_record(self._rewrite_duration, seconds, outcome=outcome)

    def memory_job_schedule_observed(self, outcome: str) -> None:
        _safe_add(self._schedule_count, 1, outcome=outcome)

    def degraded(self, dependency: str, operation: str) -> None:
        _safe_add(self._degraded_count, 1, dependency=dependency, operation=operation)

    def conversation_write_observed(self, outcome: str) -> None:
        _safe_add(self._write_count, 1, outcome=outcome)

    def request_observed(self, outcome: str, seconds: float) -> None:
        _safe_record(self._request_duration, seconds, outcome=outcome)

    def stage_observed(self, stage: str, outcome: str, seconds: float) -> None:
        _safe_record(self._stage_duration, seconds, stage=stage, outcome=outcome)

    def kira_stream_observed(
        self,
        outcome: str,
        seconds: float,
        *,
        first_event_seconds: float | None,
        first_content_seconds: float | None,
    ) -> None:
        _safe_record(self._stream_duration, seconds, outcome=outcome)
        if first_event_seconds is not None:
            _safe_record(self._first_event, first_event_seconds, outcome=outcome)
        if first_content_seconds is not None:
            _safe_record(self._first_content, first_content_seconds, outcome=outcome)


@dataclass(frozen=True, slots=True)
class WorkerGaugeSnapshot:
    pending: int = 0
    processing: int = 0
    completed: int = 0
    dead: int = 0
    oldest_pending_age_seconds: float = 0.0
    runner_active: bool = False
    queue_database_available: bool = False
    in_flight_count: int = 0
    database_backoff_seconds: float = 0.0


class _WorkerGaugeCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = WorkerGaugeSnapshot()

    def snapshot(self) -> WorkerGaugeSnapshot:
        with self._lock:
            return self._snapshot

    def update_queue(
        self,
        *,
        pending: int,
        processing: int,
        completed: int,
        dead: int,
        oldest_pending_age_seconds: float,
    ) -> None:
        with self._lock:
            current = self._snapshot
            self._snapshot = WorkerGaugeSnapshot(
                pending=pending,
                processing=processing,
                completed=completed,
                dead=dead,
                oldest_pending_age_seconds=oldest_pending_age_seconds,
                runner_active=current.runner_active,
                queue_database_available=current.queue_database_available,
                in_flight_count=current.in_flight_count,
                database_backoff_seconds=current.database_backoff_seconds,
            )

    def update_runtime(
        self,
        *,
        runner_active: bool,
        queue_database_available: bool,
        in_flight_count: int,
        database_backoff_seconds: float,
    ) -> None:
        with self._lock:
            current = self._snapshot
            self._snapshot = WorkerGaugeSnapshot(
                pending=current.pending,
                processing=current.processing,
                completed=current.completed,
                dead=current.dead,
                oldest_pending_age_seconds=current.oldest_pending_age_seconds,
                runner_active=runner_active,
                queue_database_available=queue_database_available,
                in_flight_count=in_flight_count,
                database_backoff_seconds=database_backoff_seconds,
            )


class WorkerMetrics:
    """OTel metric facade whose observable callbacks only read a locked cache."""

    def __init__(self, meter: Meter | None = None) -> None:
        self._meter = meter or metrics.NoOpMeterProvider().get_meter("kira.worker.metrics")
        self._cache = _WorkerGaugeCache()
        self._claims = _create_instrument(self._meter, METRIC_SPECS["job_claim_count"])
        self._processing = _create_instrument(self._meter, METRIC_SPECS["job_process_count"])
        self._processing_duration = _create_instrument(
            self._meter, METRIC_SPECS["job_process_duration"]
        )
        self._attempts = _create_instrument(self._meter, METRIC_SPECS["job_attempt"])
        self._lifecycle_events = _create_instrument(self._meter, METRIC_SPECS["lifecycle_event"])
        self._cleanup = _create_instrument(self._meter, METRIC_SPECS["cleanup_count"])
        self._queue_wait = _create_instrument(self._meter, METRIC_SPECS["queue_wait"])
        self._stage_duration = _create_instrument(self._meter, METRIC_SPECS["stage_duration"])
        self._formation_scope = _create_instrument(self._meter, METRIC_SPECS["formation_scope"])
        self._register_gauges()

    @property
    def cached_snapshot(self) -> WorkerGaugeSnapshot:
        return self._cache.snapshot()

    def jobs_claimed(self, *, new_count: int, reclaimed_count: int) -> None:
        if new_count:
            _safe_add(self._claims, new_count, kind="new")
        if reclaimed_count:
            _safe_add(self._claims, reclaimed_count, kind="reclaimed")

    def job_processed(
        self,
        *,
        outcome: str,
        seconds: float,
        attempt_count: int,
        lifecycle_event_count: int | None,
    ) -> None:
        _safe_add(self._processing, 1, outcome=outcome)
        _safe_record(self._processing_duration, seconds, outcome=outcome)
        _safe_record(self._attempts, attempt_count)
        if lifecycle_event_count is not None:
            _safe_record(self._lifecycle_events, lifecycle_event_count)

    def cleanup_observed(self, *, completed: int, dead: int) -> None:
        if completed:
            _safe_add(self._cleanup, completed, status="completed")
        if dead:
            _safe_add(self._cleanup, dead, status="dead")

    def queue_wait_observed(self, seconds: float) -> None:
        _safe_record(self._queue_wait, seconds)

    def stage_observed(self, stage: str, outcome: str, seconds: float) -> None:
        _safe_record(self._stage_duration, seconds, stage=stage, outcome=outcome)

    def formation_scope_observed(self, *, scope: str, origin: str, count: int) -> None:
        if count > 0:
            _safe_add(self._formation_scope, count, scope=scope, origin=origin)

    def queue_stats_observed(
        self,
        *,
        pending: int,
        processing: int,
        completed: int,
        dead: int,
        oldest_pending_age_seconds: float,
    ) -> None:
        try:
            self._cache.update_queue(
                pending=max(int(pending), 0),
                processing=max(int(processing), 0),
                completed=max(int(completed), 0),
                dead=max(int(dead), 0),
                oldest_pending_age_seconds=max(float(oldest_pending_age_seconds), 0.0),
            )
        except Exception:
            pass

    def runtime_observed(
        self,
        *,
        runner_active: bool,
        queue_database_available: bool,
        in_flight_count: int,
        database_backoff_seconds: float,
    ) -> None:
        try:
            self._cache.update_runtime(
                runner_active=bool(runner_active),
                queue_database_available=bool(queue_database_available),
                in_flight_count=max(int(in_flight_count), 0),
                database_backoff_seconds=max(float(database_backoff_seconds), 0.0),
            )
        except Exception:
            pass

    def _register_gauges(self) -> None:
        callbacks = {
            "queue_depth": self._observe_queue_depth,
            "oldest_pending_age": self._observe_oldest_pending_age,
            "runner_active": self._observe_runner_active,
            "queue_database_available": self._observe_queue_database_available,
            "in_flight": self._observe_in_flight,
            "database_backoff": self._observe_database_backoff,
        }
        for key, callback in callbacks.items():
            spec = METRIC_SPECS[key]
            try:
                self._meter.create_observable_gauge(
                    spec.name,
                    callbacks=[callback],
                    unit=spec.unit,
                )
            except Exception:
                pass

    def _observe_queue_depth(self, options: object) -> list[Observation]:
        del options
        try:
            snapshot = self._cache.snapshot()
            return [
                Observation(getattr(snapshot, status), {"status": status})
                for status in ("pending", "processing", "completed", "dead")
            ]
        except Exception:
            return []

    def _observe_oldest_pending_age(self, options: object) -> list[Observation]:
        del options
        return self._single_observation("oldest_pending_age_seconds")

    def _observe_runner_active(self, options: object) -> list[Observation]:
        del options
        try:
            return [Observation(int(self._cache.snapshot().runner_active))]
        except Exception:
            return []

    def _observe_queue_database_available(self, options: object) -> list[Observation]:
        del options
        try:
            return [Observation(int(self._cache.snapshot().queue_database_available))]
        except Exception:
            return []

    def _observe_in_flight(self, options: object) -> list[Observation]:
        del options
        return self._single_observation("in_flight_count")

    def _observe_database_backoff(self, options: object) -> list[Observation]:
        del options
        return self._single_observation("database_backoff_seconds")

    def _single_observation(self, field: str) -> list[Observation]:
        try:
            return [Observation(getattr(self._cache.snapshot(), field))]
        except Exception:
            return []
