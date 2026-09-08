from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.errors.memory_job import (
    MemoryJobLeaseLostError,
    MemoryJobQueueConfigurationError,
    MemoryJobQueueConnectionError,
    MemoryJobQueueError,
    MemoryJobQueueOperationError,
    MemoryJobQueueProtocolError,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory_job import (
    MEMORY_JOB_SCHEMA_VERSION,
    DeadMemoryJob,
    MemoryJob,
    MemoryJobPurgeResult,
    MemoryJobStats,
    MemoryJobStatus,
)


def reference() -> CompletedTurnReference:
    return CompletedTurnReference("user-1", "session-1", uuid4(), "turn-1", 42)


def job(**changes: object) -> MemoryJob:
    values: dict[str, object] = {
        "event_id": uuid4(),
        "reference": reference(),
        "attempt_count": 1,
        "lease_token": uuid4(),
        "lease_expires_at": datetime(2026, 9, 8, 1, tzinfo=UTC),
    }
    values.update(changes)
    return MemoryJob(**values)  # type: ignore[arg-type]


def test_claimed_memory_job_is_versioned_and_contains_only_stable_boundary_data() -> None:
    claimed = job(reclaimed=True)

    assert claimed.schema_version == MEMORY_JOB_SCHEMA_VERSION
    assert claimed.attempt_count == 1
    assert claimed.reclaimed is True
    assert claimed.reference.boundary_message_id == 42
    assert not hasattr(claimed, "content")


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "event"},
        {"reference": object()},
        {"attempt_count": 0},
        {"attempt_count": True},
        {"lease_token": "lease"},
        {"lease_expires_at": datetime(2026, 9, 8)},
        {"reclaimed": 1},
        {"schema_version": 2},
        {"schema_version": True},
    ],
)
def test_claimed_memory_job_rejects_invalid_boundaries(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        job(**changes)


def test_status_stats_dead_projection_and_purge_counts() -> None:
    created = datetime(2026, 9, 8, tzinfo=UTC)
    event_id = uuid4()

    stats = MemoryJobStats(2, 1, 7, 3, oldest_pending_age_seconds=4.5)
    dead = DeadMemoryJob(
        event_id,
        attempt_count=5,
        requeue_count=1,
        last_error_class="LongTermMemoryTimeoutError",
        created_at=created,
        dead_at=created + timedelta(minutes=5),
    )
    purged = MemoryJobPurgeResult(completed=10, dead=2)

    assert tuple(status.value for status in MemoryJobStatus) == (
        "pending",
        "processing",
        "completed",
        "dead",
    )
    assert stats.oldest_pending_age_seconds == 4.5
    assert dead.event_id == event_id
    assert not hasattr(dead, "user_id")
    assert purged.completed + purged.dead == 12


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MemoryJobStats(-1, 0, 0, 0),
        lambda: MemoryJobStats(True, 0, 0, 0),
        lambda: MemoryJobStats(0, 0, 0, 0, float("inf")),
        lambda: MemoryJobStats(0, 0, 0, 0, -0.1),
        lambda: DeadMemoryJob("event", 1, 0, "Error", datetime.now(UTC), datetime.now(UTC)),
        lambda: DeadMemoryJob(uuid4(), 0, 0, "Error", datetime.now(UTC), datetime.now(UTC)),
        lambda: DeadMemoryJob(uuid4(), 1, -1, "Error", datetime.now(UTC), datetime.now(UTC)),
        lambda: DeadMemoryJob(uuid4(), 1, 0, " ", datetime.now(UTC), datetime.now(UTC)),
        lambda: DeadMemoryJob(
            uuid4(), 1, 0, "not an error class", datetime.now(UTC), datetime.now(UTC)
        ),
        lambda: DeadMemoryJob(uuid4(), 1, 0, "E" * 129, datetime.now(UTC), datetime.now(UTC)),
        lambda: DeadMemoryJob(
            uuid4(),
            1,
            0,
            "Error",
            datetime(2026, 9, 8),
            datetime.now(UTC),
        ),
        lambda: DeadMemoryJob(
            uuid4(),
            1,
            0,
            "Error",
            datetime(2026, 9, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 8, tzinfo=UTC),
        ),
        lambda: MemoryJobPurgeResult(completed=-1),
        lambda: MemoryJobPurgeResult(dead=True),
    ],
)
def test_queue_projections_reject_invalid_values(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_memory_job_errors_are_typed_and_sanitized() -> None:
    errors = (
        MemoryJobQueueConfigurationError(),
        MemoryJobQueueConnectionError(),
        MemoryJobQueueOperationError(),
        MemoryJobQueueProtocolError(),
        MemoryJobLeaseLostError(),
    )

    assert all(isinstance(error, MemoryJobQueueError) for error in errors)
    assert isinstance(errors[-1], MemoryJobQueueOperationError)
    assert [str(error) for error in errors] == [
        "Memory job queue configuration is invalid",
        "Memory job queue is unavailable",
        "Memory job queue operation failed",
        "Memory job queue returned invalid data",
        "Memory job lease is no longer owned",
    ]
