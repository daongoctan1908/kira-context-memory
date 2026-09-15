"""Durable correlation and W3C context across PostgreSQL memory-job deliveries."""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.telemetry_context import TelemetryContext
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    PREVIOUS_SCHEMA_REVISION,
    conversations,
    memory_jobs,
)

pytestmark = pytest.mark.postgres_integration
USER_ID = "memory-job-trace-user"
CORRELATION_ID = "0123456789abcdef0123456789abcdef"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


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
        async with value.begin() as connection:
            await connection.execute(
                delete(conversations).where(conversations.c.user_id == USER_ID)
            )
        await value.dispose()


async def _schedule(
    engine: AsyncEngine,
    marker: str,
    carrier: TelemetryContext | None,
) -> tuple[UUID, tuple[ConversationMessage, ConversationMessage]]:
    adapter = PostgresConversationStoreAdapter(engine)
    await adapter.validate_schema()
    now = datetime.now(UTC)
    session_id = f"trace-{marker}-{uuid4()}"
    turn_id = f"turn-{marker}-{uuid4()}"
    messages = (
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.USER,
            f"question {marker}",
            now,
        ),
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.ASSISTANT,
            f"answer {marker}",
            now + timedelta(milliseconds=1),
        ),
    )
    result = await adapter.append_turn(
        USER_ID,
        *messages,
        schedule_memory=True,
        telemetry_context=carrier,
    )
    assert result.memory_job_event_id is not None
    return result.memory_job_event_id, messages


async def test_enqueue_is_atomic_and_duplicate_keeps_original_carrier(
    engine: AsyncEngine,
) -> None:
    original = TelemetryContext(
        correlation_id=CORRELATION_ID,
        traceparent=TRACEPARENT,
        tracestate="vendor=value",
    )
    event_id, messages = await _schedule(engine, "duplicate", original)
    adapter = PostgresConversationStoreAdapter(engine)
    await adapter.validate_schema()
    duplicate = await adapter.append_turn(
        USER_ID,
        *messages,
        schedule_memory=True,
        telemetry_context=TelemetryContext(correlation_id="f" * 32),
    )

    async with engine.connect() as connection:
        stored = await connection.scalar(
            select(memory_jobs.c.telemetry_context).where(memory_jobs.c.event_id == event_id)
        )

    assert duplicate.inserted is False
    assert duplicate.memory_job_event_id == event_id
    assert stored == original.to_mapping()


async def test_null_malformed_and_oversized_carriers_never_block_claim(
    engine: AsyncEngine,
) -> None:
    null_id, _ = await _schedule(engine, "null", None)
    invalid_trace_id, _ = await _schedule(
        engine,
        "invalid-trace",
        TelemetryContext(correlation_id=CORRELATION_ID),
    )
    oversized_id, _ = await _schedule(
        engine,
        "oversized",
        TelemetryContext(correlation_id="a" * 32),
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == invalid_trace_id)
            .values(
                telemetry_context={
                    "version": 1,
                    "correlation_id": CORRELATION_ID,
                    "traceparent": "invalid",
                    "tracestate": "vendor=value",
                }
            )
        )
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == oversized_id)
            .values(
                telemetry_context={
                    "version": 1,
                    "correlation_id": "a" * 32,
                    "padding": "x" * 1024,
                }
            )
        )

    queue = PostgresMemoryJobQueueAdapter(engine)
    await queue.validate_schema()
    claimed = await queue.claim_due(
        lease_owner=uuid4(),
        limit=3,
        lease_seconds=120,
        max_attempts=5,
    )
    by_id = {job.event_id: job for job in claimed}

    assert set(by_id) == {null_id, invalid_trace_id, oversized_id}
    assert by_id[null_id].telemetry_context is None
    assert by_id[oversized_id].telemetry_context is None
    assert by_id[invalid_trace_id].telemetry_context == TelemetryContext(
        correlation_id=CORRELATION_ID
    )
    for job in claimed:
        await queue.complete(job.event_id, job.lease_token, lifecycle_event_count=0)


async def test_carrier_survives_reclaim_manual_requeue_and_retry(
    engine: AsyncEngine,
) -> None:
    carrier = TelemetryContext(correlation_id=CORRELATION_ID, traceparent=TRACEPARENT)
    event_id, _ = await _schedule(engine, "redelivery", carrier)
    queue = PostgresMemoryJobQueueAdapter(engine)
    await queue.validate_schema()

    first = (
        await queue.claim_due(lease_owner=uuid4(), limit=1, lease_seconds=120, max_attempts=5)
    )[0]
    async with engine.begin() as connection:
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == event_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    reclaimed = (
        await queue.claim_due(lease_owner=uuid4(), limit=1, lease_seconds=120, max_attempts=5)
    )[0]
    assert reclaimed.reclaimed is True
    assert reclaimed.attempt_count == first.attempt_count + 1
    assert reclaimed.telemetry_context == carrier

    await queue.dead_letter(event_id, reclaimed.lease_token, error_class="SyntheticError")
    assert await queue.requeue_dead(event_id) is True
    requeued = (
        await queue.claim_due(lease_owner=uuid4(), limit=1, lease_seconds=120, max_attempts=5)
    )[0]
    assert requeued.attempt_count == 1
    assert requeued.requeue_count == 1
    assert requeued.telemetry_context == carrier

    await queue.retry(
        event_id,
        requeued.lease_token,
        next_attempt_at=datetime.now(UTC),
        error_class="SyntheticError",
    )
    retried = (
        await queue.claim_due(lease_owner=uuid4(), limit=1, lease_seconds=120, max_attempts=5)
    )[0]
    assert retried.attempt_count == 2
    assert retried.requeue_count == 1
    assert retried.telemetry_context == carrier
    await queue.complete(event_id, retried.lease_token, lifecycle_event_count=0)


def test_additive_migration_round_trips_for_bridge_rollback(migrated_database: str) -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = migrated_database
    configuration = Config("alembic.ini")
    try:
        command.downgrade(configuration, PREVIOUS_SCHEMA_REVISION)
        command.upgrade(configuration, EXPECTED_SCHEMA_REVISION)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
