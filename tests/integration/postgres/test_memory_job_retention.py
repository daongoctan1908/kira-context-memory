"""Real PostgreSQL acceptance for retry exhaustion and terminal retention cleanup."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from prometheus_client import generate_latest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobUseCase,
)
from app.domain.errors.memory import LongTermMemoryTimeoutError
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory import MemorySource
from app.domain.models.memory_job import MemoryJobPurgeResult, MemoryJobStatus
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import conversations, memory_jobs
from worker.cleanup import MemoryJobCleanupRunner
from worker.telemetry import MemoryJobTelemetry

pytestmark = pytest.mark.postgres_integration
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


@pytest.fixture(scope="module")
def migrated_database() -> str:
    database_url = _test_url()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    return database_url


@pytest.fixture
async def engine(migrated_database: str):
    value = create_async_engine(migrated_database, pool_pre_ping=True)
    try:
        yield value
    finally:
        await value.dispose()


async def _schedule(
    store: PostgresConversationStoreAdapter,
    *,
    user_id: str,
    session_id: str,
    sequence: int,
) -> UUID:
    turn_id = f"t4-15-turn-{sequence}-{uuid4()}"
    result = await store.append_turn(
        user_id,
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.USER,
            f"durable fact {sequence}",
            NOW + timedelta(milliseconds=sequence * 2),
        ),
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.ASSISTANT,
            "confirmed",
            NOW + timedelta(milliseconds=(sequence * 2) + 1),
        ),
        schedule_memory=True,
    )
    assert result.memory_job_event_id is not None
    return result.memory_job_event_id


class AlwaysTimeoutMemory:
    def __init__(self) -> None:
        self.sources: list[MemorySource] = []

    async def process_memory(self, source: MemorySource):
        self.sources.append(source)
        raise LongTermMemoryTimeoutError


class NotifyingTelemetry(MemoryJobTelemetry):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[MemoryJobPurgeResult] = []
        self.observed = asyncio.Event()

    def cleanup_observed(self, result: MemoryJobPurgeResult) -> None:
        super().cleanup_observed(result)
        self.results.append(result)
        self.observed.set()


async def test_retry_schedule_reaches_dead_on_exact_fifth_provider_attempt(
    engine: AsyncEngine,
) -> None:
    user_id = f"t4-15-retry-user-{uuid4()}"
    session_id = f"t4-15-retry-session-{uuid4()}"
    store = PostgresConversationStoreAdapter(engine)
    queue = PostgresMemoryJobQueueAdapter(engine)
    memory = AlwaysTimeoutMemory()
    event_id = await _schedule(store, user_id=user_id, session_id=session_id, sequence=1)
    try:
        processor = ProcessMemoryJobUseCase(
            ProcessMemoryUseCase(store, memory, message_limit=2),
            queue,
            max_attempts=5,
            retry_delays_seconds=(1, 5, 30, 120),
            clock=lambda: NOW,
        )

        for attempt, delay in enumerate((1, 5, 30, 120), start=1):
            async with engine.begin() as connection:
                await connection.execute(
                    update(memory_jobs)
                    .where(memory_jobs.c.event_id == event_id)
                    .values(
                        next_attempt_at=datetime(2000, 1, attempt, tzinfo=UTC),
                        created_at=datetime(2000, 1, 1, tzinfo=UTC),
                    )
                )
            claimed = await queue.claim_due(
                lease_owner=uuid4(),
                limit=1,
                lease_seconds=120,
                max_attempts=5,
            )
            assert len(claimed) == 1
            assert claimed[0].event_id == event_id
            assert claimed[0].attempt_count == attempt

            result = await processor.execute(claimed[0])

            assert result.outcome is MemoryJobProcessOutcome.RETRY
            assert result.next_attempt_at == NOW + timedelta(seconds=delay)
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            memory_jobs.c.status,
                            memory_jobs.c.attempt_count,
                            memory_jobs.c.next_attempt_at,
                            memory_jobs.c.lease_token,
                        ).where(memory_jobs.c.event_id == event_id)
                    )
                ).one()
            assert row.status == MemoryJobStatus.PENDING.value
            assert row.attempt_count == attempt
            assert row.next_attempt_at == NOW + timedelta(seconds=delay)
            assert row.lease_token is None

        async with engine.begin() as connection:
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == event_id)
                .values(next_attempt_at=datetime(2000, 1, 5, tzinfo=UTC))
            )
        fifth = (
            await queue.claim_due(
                lease_owner=uuid4(),
                limit=1,
                lease_seconds=120,
                max_attempts=5,
            )
        )[0]
        result = await processor.execute(fifth)

        assert fifth.attempt_count == 5
        assert result.outcome is MemoryJobProcessOutcome.DEAD
        assert result.next_attempt_at is None
        assert len(memory.sources) == 5
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        memory_jobs.c.status,
                        memory_jobs.c.attempt_count,
                        memory_jobs.c.last_error_class,
                        memory_jobs.c.lease_token,
                    ).where(memory_jobs.c.event_id == event_id)
                )
            ).one()
        assert row.status == MemoryJobStatus.DEAD.value
        assert row.attempt_count == 5
        assert row.last_error_class == "LongTermMemoryTimeoutError"
        assert row.lease_token is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
            )


async def test_cleanup_applies_distinct_retention_and_preserves_active_jobs(
    engine: AsyncEngine,
) -> None:
    user_id = f"t4-15-cleanup-user-{uuid4()}"
    session_id = f"t4-15-cleanup-session-{uuid4()}"
    store = PostgresConversationStoreAdapter(engine)
    queue = PostgresMemoryJobQueueAdapter(engine)
    event_ids = [
        await _schedule(store, user_id=user_id, session_id=session_id, sequence=sequence)
        for sequence in range(6)
    ]
    try:
        async with engine.begin() as connection:
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id.in_(event_ids))
                .values(
                    next_attempt_at=datetime(2000, 1, 1, tzinfo=UTC),
                    created_at=datetime(2000, 1, 1, tzinfo=UTC),
                )
            )
        claimed = await queue.claim_due(
            lease_owner=uuid4(),
            limit=5,
            lease_seconds=120,
            max_attempts=5,
        )
        assert len(claimed) == 5
        await queue.complete(claimed[0].event_id, claimed[0].lease_token, lifecycle_event_count=0)
        await queue.complete(claimed[1].event_id, claimed[1].lease_token, lifecycle_event_count=0)
        await queue.dead_letter(
            claimed[2].event_id,
            claimed[2].lease_token,
            error_class="LongTermMemoryProtocolError",
        )
        await queue.dead_letter(
            claimed[3].event_id,
            claimed[3].lease_token,
            error_class="LongTermMemoryProtocolError",
        )
        async with engine.begin() as connection:
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == claimed[0].event_id)
                .values(completed_at=NOW - timedelta(days=8))
            )
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == claimed[1].event_id)
                .values(completed_at=NOW - timedelta(days=6))
            )
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == claimed[2].event_id)
                .values(dead_at=NOW - timedelta(days=31))
            )
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == claimed[3].event_id)
                .values(dead_at=NOW - timedelta(days=29))
            )

        telemetry = NotifyingTelemetry()
        cleanup = MemoryJobCleanupRunner(
            queue,
            interval_seconds=3600,
            completed_retention_seconds=7 * 24 * 60 * 60,
            dead_retention_seconds=30 * 24 * 60 * 60,
            batch_size=10,
            database_timeout_seconds=5,
            observer=telemetry,
            clock=lambda: NOW,
        )
        task = asyncio.create_task(cleanup.run())
        await asyncio.wait_for(telemetry.observed.wait(), timeout=5)
        cleanup.request_stop()
        await asyncio.wait_for(task, timeout=2)

        assert telemetry.results == [MemoryJobPurgeResult(completed=1, dead=1)]
        payload = generate_latest(telemetry.registry).decode()
        assert 'kira_memory_job_cleanup_total{status="completed"} 1.0' in payload
        assert 'kira_memory_job_cleanup_total{status="dead"} 1.0' in payload
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(memory_jobs.c.event_id, memory_jobs.c.status).where(
                        memory_jobs.c.event_id.in_(event_ids)
                    )
                )
            ).all()
        remaining = dict(rows)
        assert claimed[0].event_id not in remaining
        assert claimed[2].event_id not in remaining
        assert remaining[claimed[1].event_id] == MemoryJobStatus.COMPLETED.value
        assert remaining[claimed[3].event_id] == MemoryJobStatus.DEAD.value
        assert remaining[claimed[4].event_id] == MemoryJobStatus.PROCESSING.value
        pending_id = (set(event_ids) - {job.event_id for job in claimed}).pop()
        assert remaining[pending_id] == MemoryJobStatus.PENDING.value
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
            )
