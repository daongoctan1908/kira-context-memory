from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobResult,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory_job import MemoryJob, MemoryJobPurgeResult, MemoryJobStats
from tests.support.otel_metrics import worker_telemetry
from worker.runner import MemoryJobRunnerSnapshot

NOW = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)


def make_job(*, reclaimed: bool = False, attempt_count: int = 1) -> MemoryJob:
    return MemoryJob(
        event_id=uuid4(),
        reference=CompletedTurnReference(
            user_id="private-user",
            session_id="private-session",
            conversation_id=uuid4(),
            turn_id="private-turn",
            boundary_message_id=42,
        ),
        attempt_count=attempt_count,
        lease_token=uuid4(),
        lease_expires_at=NOW + timedelta(minutes=2),
        reclaimed=reclaimed,
    )


def test_worker_metrics_cover_bounded_queue_and_processing_dimensions() -> None:
    telemetry, metrics = worker_telemetry()
    new_job = make_job()
    reclaimed_job = make_job(reclaimed=True, attempt_count=3)
    telemetry.jobs_claimed((new_job, reclaimed_job))
    telemetry.job_processed(
        new_job,
        ProcessMemoryJobResult(MemoryJobProcessOutcome.COMPLETED, lifecycle_event_count=2),
        0.25,
    )
    telemetry.job_processed(
        reclaimed_job,
        ProcessMemoryJobResult(MemoryJobProcessOutcome.RETRY, error_class="PrivateError"),
        1.5,
    )
    telemetry.job_processed(
        make_job(attempt_count=5),
        ProcessMemoryJobResult(MemoryJobProcessOutcome.DEAD, error_class="PrivateError"),
        2.5,
    )
    telemetry.queue_stats_observed(MemoryJobStats(4, 3, 2, 1, 9.5))
    telemetry.cleanup_observed(MemoryJobPurgeResult(completed=2, dead=1))
    telemetry.runner_observed(
        MemoryJobRunnerSnapshot(True, False, True, NOW, 0, 0.0, 3),
        queue_database_available=True,
    )

    payload = repr(metrics.snapshot())

    for status, count in (("pending", 4), ("processing", 3), ("completed", 2), ("dead", 1)):
        assert metrics.value("kira.memory.job.queue.depth", {"status": status}) == count
    assert metrics.value("kira.memory.job.claim.count", {"kind": "new"}) == 1
    assert metrics.value("kira.memory.job.claim.count", {"kind": "reclaimed"}) == 1
    for outcome in ("completed", "retry", "dead"):
        assert metrics.value("kira.memory.job.process.count", {"outcome": outcome}) == 1
    assert metrics.value("kira.memory.job.process.duration", field="count") == 3
    assert metrics.value("kira.memory.job.attempt.number", field="sum") == 9
    assert metrics.value("kira.memory.lifecycle_event.count", field="sum") == 2
    assert metrics.value("kira.memory.job.cleanup.count", {"status": "completed"}) == 2
    assert metrics.value("kira.memory.job.cleanup.count", {"status": "dead"}) == 1
    assert metrics.value("kira.memory.worker.runner.active") == 1
    assert metrics.value("kira.memory.job.queue.database.available") == 1

    for forbidden in (
        "private-user",
        "private-session",
        "private-turn",
        "PrivateError",
        str(new_job.event_id),
        str(new_job.reference.conversation_id),
    ):
        assert forbidden not in payload


def test_worker_telemetry_uses_isolated_application_meters() -> None:
    first, first_metrics = worker_telemetry()
    _, second_metrics = worker_telemetry()

    first.jobs_claimed((make_job(),))

    assert first_metrics.value("kira.memory.job.claim.count", {"kind": "new"}) == 1
    assert second_metrics.value("kira.memory.job.claim.count", {"kind": "new"}) is None


def test_skipped_deleted_source_has_bounded_metric_without_lifecycle_sample() -> None:
    telemetry, metrics = worker_telemetry()

    telemetry.job_processed(
        make_job(),
        ProcessMemoryJobResult(MemoryJobProcessOutcome.SKIPPED),
        0.01,
    )

    assert metrics.value("kira.memory.job.process.count", {"outcome": "skipped"}) == 1
    assert metrics.value("kira.memory.lifecycle_event.count", field="count") is None


def test_completed_job_emits_one_otel_completed_outcome() -> None:
    telemetry, metrics = worker_telemetry()

    telemetry.job_processed(
        make_job(),
        ProcessMemoryJobResult(MemoryJobProcessOutcome.COMPLETED, lifecycle_event_count=1),
        0.25,
    )

    points = metrics.snapshot().points["kira.memory.job.process.count"]
    assert len(points) == 1
    assert points[0].attributes == {"outcome": "completed"}
    assert points[0].value == 1


def test_formation_scope_metrics_record_valid_fallback_and_invalid_counts() -> None:
    telemetry, metrics = worker_telemetry()

    telemetry.formation_scope_observed(conversation=2, global_count=1, fallback=1, invalid=1)

    points = {
        tuple(sorted(point.attributes.items())): point.value
        for point in metrics.snapshot().points["kira.memory.scope.count"]
    }
    assert points == {
        (("origin", "valid"), ("scope", "CONVERSATION")): 2,
        (("origin", "valid"), ("scope", "GLOBAL")): 1,
        (("origin", "fallback"), ("scope", "CONVERSATION")): 1,
        (("origin", "invalid"), ("scope", "unknown")): 1,
    }


def test_formation_scope_metrics_skip_zero_counts() -> None:
    telemetry, metrics = worker_telemetry()

    telemetry.formation_scope_observed(conversation=3, global_count=0, fallback=0, invalid=0)

    points = metrics.snapshot().points["kira.memory.scope.count"]
    assert len(points) == 1
    assert points[0].attributes == {"scope": "CONVERSATION", "origin": "valid"}
    assert points[0].value == 3
