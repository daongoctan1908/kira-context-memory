"""Unit tests for PostgreSQL memory-job claiming and lifecycle transitions."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.domain.errors.memory_job import (
    MemoryJobLeaseLostError,
    MemoryJobQueueConfigurationError,
    MemoryJobQueueConnectionError,
    MemoryJobQueueOperationError,
    MemoryJobQueueProtocolError,
)
from app.domain.models.memory_job import MemoryJobStatus
from app.infrastructure.postgres.memory_job_queue import (
    PostgresMemoryJobQueueAdapter,
    _find_sqlstate,
    _is_connection,
)
from app.infrastructure.postgres.schema import EXPECTED_SCHEMA_REVISION


class FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one(self) -> dict[str, Any]:
        if len(self._rows) != 1:
            raise AssertionError("expected exactly one fake mapping")
        return self._rows[0]


class FakeScalars:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        scalar_values: list[object] | None = None,
        rowcount: int = 1,
    ) -> None:
        self._rows = rows or []
        self._scalar_values = scalar_values or []
        self.rowcount = rowcount

    def mappings(self) -> FakeMappings:
        return FakeMappings(self._rows)

    def scalars(self) -> FakeScalars:
        return FakeScalars(self._scalar_values)


class FakeConnection:
    def __init__(
        self,
        results: list[FakeResult] | None = None,
        *,
        revision: object = EXPECTED_SCHEMA_REVISION,
        error: BaseException | None = None,
    ) -> None:
        self.results = list(results or [])
        self.revision = revision
        self.error = error
        self.calls: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.calls.append(statement)
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else FakeResult()

    async def scalar(self, statement: object) -> object:
        self.calls.append(statement)
        if self.error is not None:
            raise self.error
        return self.revision


class FakeContext:
    def __init__(self, connection: FakeConnection, enter_error: BaseException | None) -> None:
        self.connection = connection
        self.enter_error = enter_error

    async def __aenter__(self) -> FakeConnection:
        if self.enter_error is not None:
            raise self.enter_error
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeEngine:
    def __init__(
        self,
        connection: FakeConnection,
        *,
        enter_error: BaseException | None = None,
    ) -> None:
        self.connection = connection
        self.enter_error = enter_error

    def connect(self) -> FakeContext:
        return FakeContext(self.connection, self.enter_error)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection, self.enter_error)


def adapter(
    connection: FakeConnection,
    *,
    enter_error: BaseException | None = None,
) -> PostgresMemoryJobQueueAdapter:
    return PostgresMemoryJobQueueAdapter(
        FakeEngine(connection, enter_error=enter_error)  # type: ignore[arg-type]
    )


def candidate(*, status: str = "pending", attempt_count: int = 0) -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "boundary_message_id": 42,
        "schema_version": 1,
        "status": status,
        "attempt_count": attempt_count,
        "conversation_id": uuid4(),
        "turn_id": "turn-1",
        "user_id": "user-1",
        "session_id": "session-1",
    }


def leased_row(attempt_count: int) -> dict[str, object]:
    return {
        "attempt_count": attempt_count,
        "lease_token": uuid4(),
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=2),
    }


async def test_validate_schema_accepts_exact_revision_and_maps_failures() -> None:
    valid = FakeConnection()
    await adapter(valid).validate_schema()
    assert "alembic_version" in str(valid.calls[0])

    with pytest.raises(MemoryJobQueueConfigurationError):
        await adapter(FakeConnection(revision="old")).validate_schema()
    with pytest.raises(MemoryJobQueueConnectionError):
        await adapter(
            FakeConnection(), enter_error=SqlAlchemyTimeoutError("unavailable")
        ).validate_schema()


async def test_claim_due_returns_typed_pending_and_reclaimed_jobs() -> None:
    pending = candidate()
    abandoned = candidate(status="processing", attempt_count=1)
    pending_lease = leased_row(1)
    reclaimed_lease = leased_row(2)
    connection = FakeConnection(
        [
            FakeResult(rows=[pending, abandoned]),
            FakeResult(rows=[pending_lease]),
            FakeResult(rows=[reclaimed_lease]),
        ]
    )
    owner = uuid4()

    jobs = await adapter(connection).claim_due(
        lease_owner=owner,
        limit=2,
        lease_seconds=120,
        max_attempts=5,
    )

    assert len(jobs) == 2
    assert jobs[0].event_id == pending["event_id"]
    assert jobs[0].attempt_count == 1
    assert jobs[0].reclaimed is False
    assert jobs[1].event_id == abandoned["event_id"]
    assert jobs[1].attempt_count == 2
    assert jobs[1].reclaimed is True
    assert jobs[1].reference.user_id == "user-1"
    assert jobs[1].reference.boundary_message_id == 42
    assert len({job.lease_token for job in jobs}) == 2
    compiled_claim = connection.calls[0].compile(dialect=postgresql.dialect())
    claim_sql = str(compiled_claim)
    assert "FOR UPDATE" in claim_sql
    assert "SKIP LOCKED" in claim_sql
    assert "memory_jobs.attempt_count <" in claim_sql
    assert "dead_exhausted_memory_jobs" in claim_sql
    assert "MemoryJobAttemptsExhaustedError" in compiled_claim.params.values()
    assert all("UPDATE memory_jobs" in str(call) for call in connection.calls[1:])


async def test_claim_due_returns_empty_tuple_without_updates() -> None:
    connection = FakeConnection([FakeResult(rows=[])])

    result = await adapter(connection).claim_due(
        lease_owner=uuid4(),
        limit=10,
        lease_seconds=120,
        max_attempts=5,
    )

    assert result == ()
    assert len(connection.calls) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lease_owner": "bad", "limit": 1, "lease_seconds": 1, "max_attempts": 1},
        {"lease_owner": uuid4(), "limit": 0, "lease_seconds": 1, "max_attempts": 1},
        {"lease_owner": uuid4(), "limit": True, "lease_seconds": 1, "max_attempts": 1},
        {"lease_owner": uuid4(), "limit": 1, "lease_seconds": 0, "max_attempts": 1},
        {"lease_owner": uuid4(), "limit": 1, "lease_seconds": float("inf"), "max_attempts": 1},
        {"lease_owner": uuid4(), "limit": 1, "lease_seconds": 1, "max_attempts": 0},
        {"lease_owner": uuid4(), "limit": 1, "lease_seconds": 1, "max_attempts": 32768},
    ],
)
async def test_claim_due_validates_bounds(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        await adapter(FakeConnection()).claim_due(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "method,kwargs,expected_status",
    [
        ("complete", {"lifecycle_event_count": 2}, MemoryJobStatus.COMPLETED.value),
        (
            "retry",
            {
                "next_attempt_at": datetime.now(UTC) + timedelta(seconds=5),
                "error_class": "LongTermMemoryTimeoutError",
            },
            MemoryJobStatus.PENDING.value,
        ),
        (
            "dead_letter",
            {"error_class": "LongTermMemoryProtocolError"},
            MemoryJobStatus.DEAD.value,
        ),
    ],
)
async def test_lease_guarded_transitions(
    method: str,
    kwargs: dict[str, object],
    expected_status: str,
) -> None:
    connection = FakeConnection([FakeResult(rowcount=1)])
    event_id = uuid4()
    token = uuid4()

    await getattr(adapter(connection), method)(event_id, token, **kwargs)

    statement = connection.calls[0]
    sql = str(statement)
    assert "UPDATE memory_jobs" in sql
    assert "memory_jobs.lease_token" in sql
    assert "memory_jobs.lease_expires_at >" in sql
    assert statement.compile().params["status"] == expected_status


@pytest.mark.parametrize("method", ["complete", "retry", "dead_letter"])
async def test_transition_rejects_lost_or_expired_lease(method: str) -> None:
    kwargs: dict[str, object]
    if method == "complete":
        kwargs = {"lifecycle_event_count": 0}
    elif method == "retry":
        kwargs = {"next_attempt_at": datetime.now(UTC), "error_class": "TimeoutError"}
    else:
        kwargs = {"error_class": "ProtocolError"}

    with pytest.raises(MemoryJobLeaseLostError):
        await getattr(adapter(FakeConnection([FakeResult(rowcount=0)])), method)(
            uuid4(), uuid4(), **kwargs
        )


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("complete", ("bad", uuid4()), {"lifecycle_event_count": 0}),
        ("complete", (uuid4(), "bad"), {"lifecycle_event_count": 0}),
        ("complete", (uuid4(), uuid4()), {"lifecycle_event_count": -1}),
        (
            "retry",
            (uuid4(), uuid4()),
            {"next_attempt_at": datetime.now(), "error_class": "TimeoutError"},
        ),
        (
            "retry",
            (uuid4(), uuid4()),
            {"next_attempt_at": datetime.now(UTC), "error_class": "contains space"},
        ),
        ("dead_letter", (uuid4(), uuid4()), {"error_class": "9Invalid"}),
    ],
)
async def test_transitions_validate_inputs(
    method: str,
    args: tuple[object, object],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        await getattr(adapter(FakeConnection()), method)(*args, **kwargs)


async def test_stats_maps_aggregate_and_clamps_negative_clock_age() -> None:
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "pending": 2,
                        "processing": 1,
                        "completed": 3,
                        "dead": 4,
                        "oldest_pending_age_seconds": -0.001,
                    }
                ]
            )
        ]
    )

    stats = await adapter(connection).stats()

    assert (stats.pending, stats.processing, stats.completed, stats.dead) == (2, 1, 3, 4)
    assert stats.oldest_pending_age_seconds == 0.0


async def test_list_dead_is_bounded_sanitized_and_deterministic() -> None:
    now = datetime.now(UTC)
    event_id = uuid4()
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "event_id": event_id,
                        "attempt_count": 5,
                        "requeue_count": 1,
                        "last_error_class": "LongTermMemoryProtocolError",
                        "created_at": now - timedelta(minutes=1),
                        "dead_at": now,
                    }
                ]
            )
        ]
    )

    jobs = await adapter(connection).list_dead(limit=10)

    assert len(jobs) == 1
    assert jobs[0].event_id == event_id
    assert jobs[0].last_error_class == "LongTermMemoryProtocolError"
    sql = str(connection.calls[0].compile(dialect=postgresql.dialect()))
    assert "ORDER BY memory_jobs.dead_at DESC" in sql
    assert "conversation" not in sql


async def test_requeue_dead_returns_boolean_and_resets_operational_state() -> None:
    updated = FakeConnection([FakeResult(rowcount=1)])
    missing = FakeConnection([FakeResult(rowcount=0)])

    assert await adapter(updated).requeue_dead(uuid4()) is True
    assert await adapter(missing).requeue_dead(uuid4()) is False
    params = updated.calls[0].compile().params
    assert params["status"] == MemoryJobStatus.PENDING.value
    assert params["attempt_count"] == 0
    assert params["last_error_class"] is None
    assert params["dead_at"] is None


async def test_purge_terminal_counts_one_bounded_mixed_batch() -> None:
    connection = FakeConnection(
        [
            FakeResult(
                scalar_values=[
                    MemoryJobStatus.COMPLETED.value,
                    MemoryJobStatus.DEAD.value,
                    MemoryJobStatus.DEAD.value,
                ]
            )
        ]
    )
    now = datetime.now(UTC)

    result = await adapter(connection).purge_terminal(
        completed_before=now,
        dead_before=now,
        limit=3,
    )

    assert result.completed == 1
    assert result.dead == 2
    sql = str(connection.calls[0].compile(dialect=postgresql.dialect()))
    assert "DELETE FROM memory_jobs" in sql
    assert "LIMIT" in sql
    assert "SKIP LOCKED" in sql


@pytest.mark.parametrize(
    "operation",
    [
        lambda queue: queue.list_dead(limit=0),
        lambda queue: queue.requeue_dead("bad"),
        lambda queue: queue.purge_terminal(
            completed_before=datetime.now(),
            dead_before=datetime.now(UTC),
            limit=1,
        ),
        lambda queue: queue.purge_terminal(
            completed_before=datetime.now(UTC),
            dead_before=datetime.now(UTC),
            limit=0,
        ),
    ],
)
async def test_admin_operations_validate_inputs(operation) -> None:
    with pytest.raises(ValueError):
        await operation(adapter(FakeConnection()))


@pytest.mark.parametrize(
    "method,result",
    [
        ("stats", FakeResult(rows=[{"pending": "bad"}])),
        (
            "list_dead",
            FakeResult(
                rows=[
                    {
                        "event_id": uuid4(),
                        "attempt_count": 0,
                        "requeue_count": 0,
                        "last_error_class": "Error",
                        "created_at": datetime.now(UTC),
                        "dead_at": datetime.now(UTC),
                    }
                ]
            ),
        ),
    ],
)
async def test_read_models_reject_malformed_database_rows(method: str, result: FakeResult) -> None:
    with pytest.raises(MemoryJobQueueProtocolError):
        if method == "stats":
            await adapter(FakeConnection([result])).stats()
        else:
            await adapter(FakeConnection([result])).list_dead(limit=1)


async def test_claim_rejects_malformed_candidate_and_rolls_back_batch() -> None:
    malformed = candidate()
    malformed["schema_version"] = 99
    connection = FakeConnection([FakeResult(rows=[malformed]), FakeResult(rows=[leased_row(1)])])

    with pytest.raises(MemoryJobQueueProtocolError):
        await adapter(connection).claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )


async def test_purge_rejects_unknown_returned_status() -> None:
    connection = FakeConnection([FakeResult(scalar_values=["pending"])])

    with pytest.raises(MemoryJobQueueProtocolError):
        await adapter(connection).purge_terminal(
            completed_before=datetime.now(UTC),
            dead_before=datetime.now(UTC),
            limit=1,
        )


class SqlStateError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("private database error")
        self.sqlstate = sqlstate


def test_error_mapping_distinguishes_configuration_connection_and_operation() -> None:
    root = SqlStateError("42P01")
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = root
    assert _find_sqlstate(wrapped) == "42P01"
    assert _is_connection("08006") is True
    assert _is_connection("57P03") is True
    assert _is_connection("23505") is False
    assert _is_connection(None) is False

    with pytest.raises(MemoryJobQueueConfigurationError):
        PostgresMemoryJobQueueAdapter._raise_mapped(wrapped)
    connection_error = DBAPIError("statement", {}, SqlStateError("08006"), True)
    with pytest.raises(MemoryJobQueueConnectionError):
        PostgresMemoryJobQueueAdapter._raise_mapped(connection_error)
    with pytest.raises(MemoryJobQueueOperationError):
        PostgresMemoryJobQueueAdapter._raise_mapped(RuntimeError("private"))
