from datetime import UTC, datetime, timedelta
from uuid import uuid4

from prometheus_client import generate_latest

from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobResult,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory_job import MemoryJob, MemoryJobPurgeResult, MemoryJobStats
from worker.runner import MemoryJobRunnerSnapshot
from worker.telemetry import MemoryJobTelemetry

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
    telemetry = MemoryJobTelemetry()
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

    payload = generate_latest(telemetry.registry).decode()

    for status, count in (("pending", 4), ("processing", 3), ("completed", 2), ("dead", 1)):
        assert f'kira_memory_job_queue_depth{{status="{status}"}} {count}.0' in payload
    assert 'kira_memory_job_claim_total{kind="new"} 1.0' in payload
    assert 'kira_memory_job_claim_total{kind="reclaimed"} 1.0' in payload
    assert 'kira_memory_job_processing_total{outcome="success"} 1.0' in payload
    assert 'kira_memory_job_processing_total{outcome="retry"} 1.0' in payload
    assert 'kira_memory_job_processing_total{outcome="dead"} 1.0' in payload
    assert "kira_memory_job_processing_duration_seconds_count" in payload
    assert "kira_memory_job_attempt_count_sum 9.0" in payload
    assert "kira_memory_job_lifecycle_event_count_sum 2.0" in payload
    assert 'kira_memory_job_cleanup_total{status="completed"} 2.0' in payload
    assert 'kira_memory_job_cleanup_total{status="dead"} 1.0' in payload
    assert "kira_memory_worker_runner_active 1.0" in payload
    assert "kira_memory_job_queue_database_available 1.0" in payload

    for forbidden in (
        "private-user",
        "private-session",
        "private-turn",
        "PrivateError",
        str(new_job.event_id),
        str(new_job.reference.conversation_id),
    ):
        assert forbidden not in payload


def test_worker_telemetry_uses_one_isolated_registry_per_process_instance() -> None:
    first = MemoryJobTelemetry()
    second = MemoryJobTelemetry()

    first.jobs_claimed((make_job(),))

    assert first.registry is not second.registry
    assert 'kira_memory_job_claim_total{kind="new"} 1.0' in generate_latest(first.registry).decode()
    assert (
        'kira_memory_job_claim_total{kind="new"} 0.0' in generate_latest(second.registry).decode()
    )
