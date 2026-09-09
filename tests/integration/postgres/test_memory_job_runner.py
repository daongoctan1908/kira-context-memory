"""Real PostgreSQL lease-reclaim acceptance for the concurrent Worker runner."""

import asyncio
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
    ProcessMemoryJobResult,
    ProcessMemoryJobUseCase,
)
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory import MemoryLifecycleEvent, MemoryProcessResult, MemorySource
from app.domain.models.memory_job import MemoryJob, MemoryJobStatus
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import conversations, memory_jobs
from worker.runner import MemoryJobRunner

pytestmark = pytest.mark.postgres_integration
NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


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
    def __init__(self) -> None:
        self.sources: list[MemorySource] = []

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        self.sources.append(source)
        return MemoryProcessResult((MemoryLifecycleEvent("ADD", "memory-1", "durable fact"),))


class NotifyingProcessor:
    def __init__(self, use_case: ProcessMemoryJobUseCase) -> None:
        self.use_case = use_case
        self.jobs: list[MemoryJob] = []
        self.completed = asyncio.Event()

    async def execute(self, job: MemoryJob) -> ProcessMemoryJobResult:
        self.jobs.append(job)
        result = await self.use_case.execute(job)
        self.completed.set()
        return result


async def test_runner_reclaims_expired_lease_and_completes_exact_boundary(
    engine: AsyncEngine,
) -> None:
    user_id = f"t4-13-user-{uuid4()}"
    session_id = f"t4-13-session-{uuid4()}"
    turn_id = f"t4-13-turn-{uuid4()}"
    store = PostgresConversationStoreAdapter(engine)
    queue = PostgresMemoryJobQueueAdapter(engine)
    try:
        appended = await store.append_turn(
            user_id,
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.USER,
                "remember the reclaimed source",
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
        assert appended.memory_job_event_id is not None
        async with engine.begin() as connection:
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == appended.memory_job_event_id)
                .values(
                    next_attempt_at=datetime(2000, 1, 1, tzinfo=UTC),
                    created_at=datetime(2000, 1, 1, tzinfo=UTC),
                )
            )

        abandoned = await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )
        assert len(abandoned) == 1
        assert abandoned[0].event_id == appended.memory_job_event_id
        async with engine.begin() as connection:
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == appended.memory_job_event_id)
                .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
            )

        memory = FormationMemory()
        process_memory = ProcessMemoryUseCase(store, memory, message_limit=2)
        processor = NotifyingProcessor(
            ProcessMemoryJobUseCase(
                process_memory,
                queue,
                max_attempts=5,
                retry_delays_seconds=(1, 5, 30, 120),
            )
        )
        runner = MemoryJobRunner(
            queue,
            processor,
            poll_interval_seconds=0.01,
            batch_size=1,
            concurrency=1,
            lease_seconds=120,
            max_attempts=5,
            database_timeout_seconds=5,
            shutdown_grace_seconds=1,
        )

        run_task = asyncio.create_task(runner.run())
        await asyncio.wait_for(processor.completed.wait(), timeout=5)
        runner.request_stop()
        await asyncio.wait_for(run_task, timeout=2)

        assert len(processor.jobs) == 1
        reclaimed = processor.jobs[0]
        assert reclaimed.event_id == appended.memory_job_event_id
        assert reclaimed.reclaimed is True
        assert reclaimed.attempt_count == 2
        assert memory.sources[0].reference == appended.reference
        assert [message.content for message in memory.sources[0].messages] == [
            "remember the reclaimed source",
            "confirmed",
        ]
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        memory_jobs.c.status,
                        memory_jobs.c.attempt_count,
                        memory_jobs.c.lifecycle_event_count,
                        memory_jobs.c.lease_token,
                    ).where(memory_jobs.c.event_id == appended.memory_job_event_id)
                )
            ).one()
        assert row.status == MemoryJobStatus.COMPLETED.value
        assert row.attempt_count == 2
        assert row.lifecycle_event_count == 1
        assert row.lease_token is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
            )
