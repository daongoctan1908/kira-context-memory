"""Real PostgreSQL acceptance for exact-boundary memory-job processing."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobUseCase,
)
from app.domain.errors.memory import LongTermMemoryProtocolError, LongTermMemoryTimeoutError
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory import MemoryLifecycleEvent, MemoryProcessResult, MemorySource
from app.domain.models.memory_job import MemoryJobStatus
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import conversations, memory_jobs

pytestmark = pytest.mark.postgres_integration
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


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


class FormationMemory:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.sources: list[MemorySource] = []

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        self.sources.append(source)
        if self.error is not None:
            raise self.error
        return MemoryProcessResult(
            (
                MemoryLifecycleEvent("ADD", "memory-1", "fact"),
                MemoryLifecycleEvent("NONE"),
            )
        )


@pytest.mark.parametrize(
    ("formation_error", "expected_outcome", "expected_status"),
    [
        (None, MemoryJobProcessOutcome.COMPLETED, MemoryJobStatus.COMPLETED),
        (
            LongTermMemoryTimeoutError(),
            MemoryJobProcessOutcome.RETRY,
            MemoryJobStatus.PENDING,
        ),
        (
            LongTermMemoryProtocolError(),
            MemoryJobProcessOutcome.DEAD,
            MemoryJobStatus.DEAD,
        ),
    ],
)
async def test_exact_postgres_boundary_transitions_to_complete_retry_or_dead(
    engine: AsyncEngine,
    formation_error: Exception | None,
    expected_outcome: MemoryJobProcessOutcome,
    expected_status: MemoryJobStatus,
) -> None:
    user_id = f"t4-12-user-{uuid4()}"
    session_id = f"t4-12-session-{uuid4()}"
    turn_id = f"t4-12-turn-{uuid4()}"
    store = PostgresConversationStoreAdapter(engine)
    queue = PostgresMemoryJobQueueAdapter(engine)
    try:
        append_result = await store.append_turn(
            user_id,
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.USER,
                "remember this exact source",
                NOW,
            ),
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.ASSISTANT,
                "confirmed",
                NOW + timedelta(milliseconds=1),
            ),
            schedule_memory=True,
        )
        assert append_result.memory_job_event_id is not None
        async with engine.begin() as connection:
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == append_result.memory_job_event_id)
                .values(
                    next_attempt_at=NOW - timedelta(days=1),
                    created_at=NOW - timedelta(days=1),
                )
            )
        claimed = await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )
        assert len(claimed) == 1
        job = claimed[0]
        assert job.event_id == append_result.memory_job_event_id

        memory = FormationMemory(formation_error)
        process_memory = ProcessMemoryUseCase(store, memory, message_limit=2)
        use_case = ProcessMemoryJobUseCase(
            process_memory,
            queue,
            max_attempts=5,
            retry_delays_seconds=(1, 5, 30, 120),
            clock=lambda: NOW,
        )

        result = await use_case.execute(job)

        assert result.outcome is expected_outcome
        assert memory.sources[0].reference == append_result.reference
        assert [message.content for message in memory.sources[0].messages] == [
            "remember this exact source",
            "confirmed",
        ]
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        memory_jobs.c.status,
                        memory_jobs.c.last_error_class,
                        memory_jobs.c.lifecycle_event_count,
                        memory_jobs.c.next_attempt_at,
                        memory_jobs.c.lease_token,
                    ).where(memory_jobs.c.event_id == job.event_id)
                )
            ).one()

        assert row.status == expected_status.value
        assert row.lease_token is None
        if expected_outcome is MemoryJobProcessOutcome.COMPLETED:
            assert row.lifecycle_event_count == 2
            assert row.last_error_class is None
        elif expected_outcome is MemoryJobProcessOutcome.RETRY:
            assert row.lifecycle_event_count is None
            assert row.last_error_class == "LongTermMemoryTimeoutError"
            assert row.next_attempt_at == NOW + timedelta(seconds=1)
        else:
            assert row.lifecycle_event_count is None
            assert row.last_error_class == "LongTermMemoryProtocolError"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
            )
