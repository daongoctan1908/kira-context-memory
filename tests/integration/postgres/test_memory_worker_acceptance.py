"""End-to-end PostgreSQL acceptance for the Week 4 memory Worker runtime."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from prometheus_client import generate_latest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import (
    ProcessMemoryJobResult,
    ProcessMemoryJobUseCase,
)
from app.domain.errors.memory import LongTermMemoryTimeoutError
from app.domain.errors.memory_job import MemoryJobLeaseLostError
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import MemoryLifecycleEvent, MemoryProcessResult, MemorySource
from app.domain.models.memory_job import MemoryJob, MemoryJobStatus
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import conversations, memory_jobs
from worker.main import create_app
from worker.runner import MemoryJobRunner
from worker.settings import WorkerSettings
from worker.telemetry import MemoryJobTelemetry

pytestmark = pytest.mark.postgres_integration


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


def _worker_settings(database_url: str) -> WorkerSettings:
    memory_database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return WorkerSettings(
        _env_file=None,
        database_url=database_url,
        memory_database_url=memory_database_url,
        memory_embedding_base_url="http://embedding.test",
        memory_embedding_model="embedding-model",
        memory_embedding_dims=3,
        memory_llm_base_url="http://memory-llm.test",
        memory_llm_model="memory-model",
        conversation_operation_timeout_seconds=0.1,
        memory_operation_timeout_seconds=0.1,
        memory_job_poll_interval_seconds=0.01,
        memory_job_batch_size=1,
        memory_job_concurrency=1,
        memory_job_lease_seconds=1,
        memory_job_max_attempts=2,
        memory_job_retry_delays_seconds=(0.02,),
        memory_job_db_timeout_seconds=1,
        memory_job_shutdown_grace_seconds=0.05,
        memory_job_metrics_refresh_seconds=0.01,
        memory_job_cleanup_interval_seconds=3600,
    )


async def _schedule_turn(
    store: PostgresConversationStoreAdapter,
    *,
    user_id: str,
    session_id: str,
    marker: str,
) -> tuple[UUID, CompletedTurnReference]:
    timestamp = datetime.now(UTC)
    turn_id = f"t4-17-turn-{marker}-{uuid4()}"
    result = await store.append_turn(
        user_id,
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.USER,
            f"durable user fact {marker}",
            timestamp,
        ),
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.ASSISTANT,
            f"confirmed {marker}",
            timestamp + timedelta(milliseconds=1),
        ),
        schedule_memory=True,
    )
    assert result.memory_job_event_id is not None
    return result.memory_job_event_id, result.reference


async def _wait_for_status(
    engine: AsyncEngine,
    event_id: UUID,
    expected: MemoryJobStatus,
    *,
    timeout_seconds: float = 5,
):
    async with asyncio.timeout(timeout_seconds):
        while True:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            memory_jobs.c.status,
                            memory_jobs.c.attempt_count,
                            memory_jobs.c.lifecycle_event_count,
                            memory_jobs.c.lease_token,
                        ).where(memory_jobs.c.event_id == event_id)
                    )
                ).one()
            if row.status == expected.value:
                return row
            await asyncio.sleep(0.01)


async def _wait_for_ready(client: httpx.AsyncClient, expected_status: int) -> httpx.Response:
    async with asyncio.timeout(5):
        while True:
            response = await client.get("/ready")
            if response.status_code == expected_status:
                return response
            await asyncio.sleep(0.01)


class RetryOnceMemory:
    def __init__(self) -> None:
        self.sources: list[MemorySource] = []
        self.completed = asyncio.Event()

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        self.sources.append(source)
        if len(self.sources) == 1:
            raise LongTermMemoryTimeoutError
        self.completed.set()
        return MemoryProcessResult((MemoryLifecycleEvent("ADD", "memory-1", "durable fact"),))


class BlockingMemory:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class ReleasableMemory:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        self.started.set()
        await self.release.wait()
        return MemoryProcessResult((MemoryLifecycleEvent("ADD", "memory-2", "durable fact"),))


class RecordingProcessor:
    def __init__(self, use_case: ProcessMemoryJobUseCase) -> None:
        self._use_case = use_case
        self.jobs: list[MemoryJob] = []
        self.completed = asyncio.Event()

    async def execute(self, job: MemoryJob) -> ProcessMemoryJobResult:
        self.jobs.append(job)
        result = await self._use_case.execute(job)
        self.completed.set()
        return result


async def test_worker_http_runtime_retries_then_completes_exact_postgres_boundary(
    engine: AsyncEngine,
    migrated_database: str,
) -> None:
    user_id = f"t4-17-retry-user-{uuid4()}"
    session_id = f"t4-17-retry-session-{uuid4()}"
    store = PostgresConversationStoreAdapter(engine)
    queue = PostgresMemoryJobQueueAdapter(engine)
    memory = RetryOnceMemory()
    event_id, reference = await _schedule_turn(
        store,
        user_id=user_id,
        session_id=session_id,
        marker="retry",
    )
    settings = _worker_settings(migrated_database)

    @asynccontextmanager
    async def dependencies(
        resolved_settings: WorkerSettings,
        **kwargs: object,
    ) -> AsyncIterator[SimpleNamespace]:
        process_memory = ProcessMemoryUseCase(store, memory, message_limit=2)
        process_job = ProcessMemoryJobUseCase(
            process_memory,
            queue,
            max_attempts=resolved_settings.memory_job_max_attempts,
            retry_delays_seconds=resolved_settings.memory_job_retry_delays_seconds,
        )
        runner = MemoryJobRunner(
            queue,
            process_job,
            poll_interval_seconds=resolved_settings.memory_job_poll_interval_seconds,
            batch_size=resolved_settings.memory_job_batch_size,
            concurrency=resolved_settings.memory_job_concurrency,
            lease_seconds=resolved_settings.memory_job_lease_seconds,
            max_attempts=resolved_settings.memory_job_max_attempts,
            database_timeout_seconds=resolved_settings.memory_job_db_timeout_seconds,
            shutdown_grace_seconds=resolved_settings.memory_job_shutdown_grace_seconds,
            observer=kwargs["job_observer"],
        )
        yield SimpleNamespace(runner=runner, memory_job_queue=queue)

    app = create_app(settings=settings, dependency_lifespan=dependencies)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://worker.test",
            ) as client:
                await asyncio.wait_for(memory.completed.wait(), timeout=5)
                row = await _wait_for_status(engine, event_id, MemoryJobStatus.COMPLETED)
                ready = await _wait_for_ready(client, 200)
                health = await client.get("/health")
                metrics = await client.get("/metrics")

        assert ready.json() == {"status": "ready"}
        assert health.status_code == 200
        assert row.attempt_count == 2
        assert row.lifecycle_event_count == 1
        assert row.lease_token is None
        assert len(memory.sources) == 2
        assert all(source.reference == reference for source in memory.sources)
        assert [message.content for message in memory.sources[-1].messages] == [
            "durable user fact retry",
            "confirmed retry",
        ]
        assert 'kira_memory_job_processing_total{outcome="retry"} 1.0' in metrics.text
        assert 'kira_memory_job_processing_total{outcome="success"} 1.0' in metrics.text
        assert 'kira_memory_job_claim_total{kind="new"} 2.0' in metrics.text
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
            )


async def test_grace_expiry_preserves_job_for_reclaim_and_rejects_stale_lease(
    engine: AsyncEngine,
) -> None:
    user_id = f"t4-17-reclaim-user-{uuid4()}"
    session_id = f"t4-17-reclaim-session-{uuid4()}"
    store = PostgresConversationStoreAdapter(engine)
    queue = PostgresMemoryJobQueueAdapter(engine)
    event_id, _ = await _schedule_turn(
        store,
        user_id=user_id,
        session_id=session_id,
        marker="reclaim",
    )
    blocked_memory = BlockingMemory()
    first_processor = RecordingProcessor(
        ProcessMemoryJobUseCase(
            ProcessMemoryUseCase(store, blocked_memory, message_limit=2),
            queue,
            max_attempts=2,
            retry_delays_seconds=(0.02,),
        )
    )
    first_runner = MemoryJobRunner(
        queue,
        first_processor,
        poll_interval_seconds=0.01,
        batch_size=1,
        concurrency=1,
        lease_seconds=120,
        max_attempts=2,
        database_timeout_seconds=1,
        shutdown_grace_seconds=0.01,
    )
    first_task: asyncio.Task[None] | None = None
    replacement_runner: MemoryJobRunner | None = None
    replacement_task: asyncio.Task[None] | None = None
    replacement_memory: ReleasableMemory | None = None

    try:
        first_task = asyncio.create_task(first_runner.run())
        await asyncio.wait_for(blocked_memory.started.wait(), timeout=5)
        first_runner.request_stop()
        await asyncio.wait_for(first_task, timeout=2)
        await asyncio.wait_for(blocked_memory.cancelled.wait(), timeout=1)

        assert len(first_processor.jobs) == 1
        stale_token = first_processor.jobs[0].lease_token
        async with engine.connect() as connection:
            abandoned = (
                await connection.execute(
                    select(
                        memory_jobs.c.status,
                        memory_jobs.c.attempt_count,
                        memory_jobs.c.lease_token,
                    ).where(memory_jobs.c.event_id == event_id)
                )
            ).one()
        assert abandoned.status == MemoryJobStatus.PROCESSING.value
        assert abandoned.attempt_count == 1
        assert abandoned.lease_token == stale_token

        async with engine.begin() as connection:
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == event_id)
                .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
            )

        replacement_memory = ReleasableMemory()
        replacement_processor = RecordingProcessor(
            ProcessMemoryJobUseCase(
                ProcessMemoryUseCase(store, replacement_memory, message_limit=2),
                queue,
                max_attempts=2,
                retry_delays_seconds=(0.02,),
            )
        )
        telemetry = MemoryJobTelemetry()
        replacement_runner = MemoryJobRunner(
            queue,
            replacement_processor,
            poll_interval_seconds=0.01,
            batch_size=1,
            concurrency=1,
            lease_seconds=120,
            max_attempts=2,
            database_timeout_seconds=1,
            shutdown_grace_seconds=1,
            observer=telemetry,
        )
        replacement_task = asyncio.create_task(replacement_runner.run())
        await asyncio.wait_for(replacement_memory.started.wait(), timeout=5)

        with pytest.raises(MemoryJobLeaseLostError):
            await queue.complete(event_id, stale_token, lifecycle_event_count=99)

        replacement_memory.release.set()
        await asyncio.wait_for(replacement_processor.completed.wait(), timeout=5)
        replacement_runner.request_stop()
        await asyncio.wait_for(replacement_task, timeout=2)

        completed = await _wait_for_status(engine, event_id, MemoryJobStatus.COMPLETED)
        assert len(replacement_processor.jobs) == 1
        assert replacement_processor.jobs[0].reclaimed is True
        assert replacement_processor.jobs[0].attempt_count == 2
        assert completed.attempt_count == 2
        assert completed.lifecycle_event_count == 1
        assert completed.lease_token is None
        metrics = generate_latest(telemetry.registry).decode()
        assert 'kira_memory_job_claim_total{kind="reclaimed"} 1.0' in metrics
        assert 'kira_memory_job_processing_total{outcome="success"} 1.0' in metrics
    finally:
        first_runner.request_stop()
        if replacement_memory is not None:
            replacement_memory.release.set()
        if replacement_runner is not None:
            replacement_runner.request_stop()
        pending_tasks = tuple(
            task for task in (first_task, replacement_task) if task is not None and not task.done()
        )
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == user_id,
                    conversations.c.session_id == session_id,
                )
            )
