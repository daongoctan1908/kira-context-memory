import argparse
import asyncio
import json
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

import worker.job_admin as job_admin
from app.domain.errors.memory_job import (
    MemoryJobQueueConnectionError,
    MemoryJobQueueProtocolError,
)
from app.domain.models.memory_job import DeadMemoryJob, MemoryJobStats
from worker.job_admin import AdminExitCode, execute_command, parse_args, run
from worker.settings import MemoryJobAdminSettings

NOW = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)


def make_settings(**overrides: object) -> MemoryJobAdminSettings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://operator:private@postgres.test/kira",
    }
    values.update(overrides)
    return MemoryJobAdminSettings(**values)  # type: ignore[arg-type]


class FakeQueue:
    def __init__(self) -> None:
        self.schema_validated = False
        self.stats_result: object = MemoryJobStats(2, 1, 4, 3, 7.5)
        self.dead_result: object = ()
        self.requeue_result: object = True
        self.dead_limits: list[int] = []
        self.requeue_ids: list[UUID] = []

    async def validate_schema(self) -> None:
        self.schema_validated = True

    async def stats(self):
        return self.stats_result

    async def list_dead(self, *, limit: int):
        self.dead_limits.append(limit)
        return self.dead_result

    async def requeue_dead(self, event_id: UUID):
        self.requeue_ids.append(event_id)
        return self.requeue_result


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


async def test_stats_emits_stable_sanitized_json() -> None:
    output = StringIO()

    result = await execute_command(
        parse_args(["stats"]),
        FakeQueue(),  # type: ignore[arg-type]
        timeout_seconds=1,
        output=output,
    )

    assert result is AdminExitCode.SUCCESS
    assert json.loads(output.getvalue()) == {
        "completed": 4,
        "dead": 3,
        "oldest_pending_age_seconds": 7.5,
        "pending": 2,
        "processing": 1,
    }


async def test_list_dead_is_bounded_and_contains_only_operator_projection() -> None:
    event_id = uuid4()
    queue = FakeQueue()
    queue.dead_result = (
        DeadMemoryJob(
            event_id=event_id,
            attempt_count=5,
            requeue_count=2,
            last_error_class="LongTermMemoryTimeoutError",
            created_at=NOW,
            dead_at=NOW,
        ),
    )
    output = StringIO()

    result = await execute_command(
        parse_args(["list-dead", "--limit", "25"]),
        queue,  # type: ignore[arg-type]
        timeout_seconds=1,
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert result is AdminExitCode.SUCCESS
    assert queue.dead_limits == [25]
    assert payload == {
        "count": 1,
        "items": [
            {
                "attempt_count": 5,
                "created_at": "2026-09-10T10:00:00Z",
                "dead_at": "2026-09-10T10:00:00Z",
                "event_id": str(event_id),
                "last_error_class": "LongTermMemoryTimeoutError",
                "requeue_count": 2,
            }
        ],
        "limit": 25,
    }
    rendered = output.getvalue()
    for forbidden in (
        "user_id",
        "session_id",
        "conversation_id",
        "turn_id",
        "boundary_message_id",
        "content",
        "prompt",
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("requeued", "expected"),
    [
        (True, AdminExitCode.SUCCESS),
        (False, AdminExitCode.NOT_REQUEUED),
    ],
)
async def test_requeue_targets_exactly_one_explicit_event(
    requeued: bool,
    expected: AdminExitCode,
) -> None:
    event_id = uuid4()
    queue = FakeQueue()
    queue.requeue_result = requeued
    output = StringIO()

    result = await execute_command(
        parse_args(["requeue", "--event-id", str(event_id)]),
        queue,  # type: ignore[arg-type]
        timeout_seconds=1,
        output=output,
    )

    assert result is expected
    assert queue.requeue_ids == [event_id]
    assert json.loads(output.getvalue()) == {
        "event_id": str(event_id),
        "requeued": requeued,
    }


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["list-dead", "--limit", "0"],
        ["list-dead", "--limit", "1001"],
        ["list-dead", "--limit", "many"],
        ["requeue"],
        ["requeue", "--event-id", "not-a-uuid"],
    ],
)
def test_parser_rejects_missing_or_unbounded_operator_targets(
    argv: list[str],
    capsys,
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_args(argv)
    assert error.value.code == int(AdminExitCode.USAGE_OR_CONFIGURATION)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "invalid_invocation"}
    assert "not-a-uuid" not in captured.err


@pytest.mark.parametrize(
    ("command", "invalid_result"),
    [
        (["stats"], object()),
        (["list-dead"], [object()]),
        (["requeue", "--event-id", "cb96698a-e5e5-40f1-83af-f86ab754a58b"], 1),
    ],
)
async def test_cli_rejects_invalid_port_results(
    command: list[str],
    invalid_result: object,
) -> None:
    queue = FakeQueue()
    if command[0] == "stats":
        queue.stats_result = invalid_result
    elif command[0] == "list-dead":
        queue.dead_result = invalid_result
    else:
        queue.requeue_result = invalid_result

    with pytest.raises(MemoryJobQueueProtocolError):
        await execute_command(
            parse_args(command),
            queue,  # type: ignore[arg-type]
            timeout_seconds=1,
            output=StringIO(),
        )


async def test_database_timeout_cancels_command_and_maps_to_connection_error() -> None:
    cancelled = asyncio.Event()

    class BlockingQueue(FakeQueue):
        async def stats(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    with pytest.raises(MemoryJobQueueConnectionError):
        await execute_command(
            parse_args(["stats"]),
            BlockingQueue(),  # type: ignore[arg-type]
            timeout_seconds=0.001,
            output=StringIO(),
        )
    assert cancelled.is_set()


async def test_run_validates_schema_and_disposes_only_owned_engine() -> None:
    settings = make_settings()
    owned_engine = FakeEngine()
    owned_queue = FakeQueue()
    output = StringIO()

    result = await run(
        parse_args(["stats"]),
        settings=settings,
        output=output,
        engine_factory=Mock(return_value=owned_engine),  # type: ignore[arg-type]
        queue_factory=Mock(return_value=owned_queue),  # type: ignore[arg-type]
    )

    assert result is AdminExitCode.SUCCESS
    assert owned_queue.schema_validated is True
    assert owned_engine.disposed is True

    injected_engine = FakeEngine()
    injected_queue = FakeQueue()
    await run(
        parse_args(["list-dead"]),
        settings=settings,
        engine=injected_engine,  # type: ignore[arg-type]
        output=StringIO(),
        queue_factory=Mock(return_value=injected_queue),  # type: ignore[arg-type]
    )
    assert injected_queue.schema_validated is True
    assert injected_engine.disposed is False


async def test_run_disposes_owned_engine_when_schema_validation_fails() -> None:
    engine = FakeEngine()

    class InvalidQueue(FakeQueue):
        async def validate_schema(self) -> None:
            raise MemoryJobQueueProtocolError

    with pytest.raises(MemoryJobQueueProtocolError):
        await run(
            parse_args(["stats"]),
            settings=make_settings(),
            engine_factory=Mock(return_value=engine),  # type: ignore[arg-type]
            queue_factory=Mock(return_value=InvalidQueue()),  # type: ignore[arg-type]
            output=StringIO(),
        )
    assert engine.disposed is True


async def test_run_disposes_owned_engine_when_queue_construction_fails() -> None:
    engine = FakeEngine()

    def fail_queue_construction(resolved_engine: object):
        raise RuntimeError("private constructor detail")

    with pytest.raises(RuntimeError, match="private constructor detail"):
        await run(
            parse_args(["stats"]),
            settings=make_settings(),
            engine_factory=Mock(return_value=engine),  # type: ignore[arg-type]
            queue_factory=fail_queue_construction,  # type: ignore[arg-type]
            output=StringIO(),
        )
    assert engine.disposed is True


def test_admin_settings_need_no_provider_configuration_and_redact_database_secret() -> None:
    settings = make_settings()

    assert settings.postgres_pool_size == 2
    assert settings.postgres_max_overflow == 0
    assert not hasattr(settings, "memory_database_url")
    assert not hasattr(settings, "memory_embedding_base_url")
    assert not hasattr(settings, "memory_llm_base_url")
    assert "private" not in repr(settings)


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_error"),
    [
        (
            MemoryJobQueueConnectionError(),
            AdminExitCode.DEPENDENCY_ERROR,
            "queue_unavailable",
        ),
        (
            MemoryJobQueueProtocolError(),
            AdminExitCode.DEPENDENCY_ERROR,
            "invalid_queue_response",
        ),
        (RuntimeError("private provider detail"), AdminExitCode.INTERNAL_ERROR, "internal_error"),
    ],
)
def test_main_returns_stable_exit_codes_without_exception_details(
    monkeypatch,
    capsys,
    error: Exception,
    expected_code: AdminExitCode,
    expected_error: str,
) -> None:
    async def fail(args: argparse.Namespace):
        raise error

    monkeypatch.setattr(job_admin, "run", fail)

    assert job_admin.main(["stats"]) == int(expected_code)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": expected_error}
    assert "private provider detail" not in captured.err
