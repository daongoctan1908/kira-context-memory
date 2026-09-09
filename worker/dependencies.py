"""Composition root and dependency ownership for the memory Worker process."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import ProcessMemoryJobUseCase
from app.domain.errors.memory import LongTermMemoryTimeoutError
from app.domain.errors.memory_job import MemoryJobQueueConnectionError
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.long_term_memory import LongTermMemoryPort
from app.domain.ports.memory_job_queue import MemoryJobQueuePort
from app.infrastructure.memory.mem0_adapter import Mem0Adapter
from app.infrastructure.memory.postgres_admin import validate_memory_schema
from app.infrastructure.postgres.client import create_postgres_engine
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.managed_store import ManagedPostgresConversationStore
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from worker.settings import WorkerSettings, get_worker_settings


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    """Validated dependencies shared by the future poller and Worker HTTP app."""

    settings: WorkerSettings
    conversation_store: ConversationStorePort
    memory_job_queue: MemoryJobQueuePort
    long_term_memory: LongTermMemoryPort
    process_memory: ProcessMemoryUseCase
    process_memory_job: ProcessMemoryJobUseCase


@asynccontextmanager
async def worker_dependency_lifespan(
    settings: WorkerSettings | None = None,
    *,
    postgres_engine: AsyncEngine | None = None,
    long_term_memory: LongTermMemoryPort | None = None,
) -> AsyncIterator[WorkerDependencies]:
    """Validate, construct, and close dependencies owned by one Worker process."""
    resolved_settings = settings or get_worker_settings()
    owned_engine: AsyncEngine | None = None
    owned_memory: Mem0Adapter | None = None

    try:
        resolved_engine = postgres_engine
        if resolved_engine is None:
            owned_engine = create_postgres_engine(resolved_settings)
            resolved_engine = owned_engine

        conversation_store = ManagedPostgresConversationStore(
            PostgresConversationStoreAdapter(resolved_engine),
            resolved_settings.conversation_operation_timeout_seconds,
        )
        memory_job_queue = PostgresMemoryJobQueueAdapter(resolved_engine)

        await conversation_store.validate_schema()
        try:
            async with asyncio.timeout(resolved_settings.memory_job_db_timeout_seconds):
                await memory_job_queue.validate_schema()
        except TimeoutError:
            raise MemoryJobQueueConnectionError from None

        try:
            async with asyncio.timeout(resolved_settings.memory_job_db_timeout_seconds):
                await validate_memory_schema(resolved_settings)
        except TimeoutError:
            raise LongTermMemoryTimeoutError from None

        resolved_memory = long_term_memory
        if resolved_memory is None:
            owned_memory = Mem0Adapter.from_settings(resolved_settings)
            resolved_memory = owned_memory

        process_memory = ProcessMemoryUseCase(
            conversation_store,
            resolved_memory,
            message_limit=resolved_settings.memory_formation_message_limit,
        )
        process_memory_job = ProcessMemoryJobUseCase(
            process_memory,
            memory_job_queue,
            max_attempts=resolved_settings.memory_job_max_attempts,
            retry_delays_seconds=resolved_settings.memory_job_retry_delays_seconds,
        )
        yield WorkerDependencies(
            settings=resolved_settings,
            conversation_store=conversation_store,
            memory_job_queue=memory_job_queue,
            long_term_memory=resolved_memory,
            process_memory=process_memory,
            process_memory_job=process_memory_job,
        )
    finally:
        try:
            if owned_memory is not None:
                owned_memory.close()
        finally:
            if owned_engine is not None:
                await owned_engine.dispose()
