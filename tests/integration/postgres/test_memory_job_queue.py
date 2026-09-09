"""Real PostgreSQL concurrency and lifecycle tests for the memory-job queue."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.errors.memory_job import MemoryJobLeaseLostError
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory_job import MemoryJobStatus
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.memory_job_queue import PostgresMemoryJobQueueAdapter
from app.infrastructure.postgres.schema import conversations, memory_jobs

pytestmark = pytest.mark.postgres_integration
USER_ID = "memory-job-queue-user"


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


@pytest.fixture
async def session_id(engine: AsyncEngine):
    value = f"memory-job-queue-{uuid4()}"
    try:
        yield value
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id == USER_ID,
                    conversations.c.session_id == value,
                )
            )


async def _schedule(
    engine: AsyncEngine,
    session_id: str,
    number: int,
    *,
    user_id: str = USER_ID,
) -> UUID:
    timestamp = datetime.now(UTC)
    turn_id = f"{session_id}-turn-{number}-{uuid4()}"
    result = await PostgresConversationStoreAdapter(engine).append_turn(
        user_id,
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.USER,
            f"synthetic question {number}",
            timestamp,
        ),
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.ASSISTANT,
            f"synthetic answer {number}",
            timestamp + timedelta(milliseconds=1),
        ),
        schedule_memory=True,
    )
    assert result.memory_job_event_id is not None
    return result.memory_job_event_id


async def test_claim_order_is_deterministic_by_due_creation_and_event_id(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    event_ids = [await _schedule(engine, session_id, number) for number in range(4)]
    anchor = datetime.now(UTC) - timedelta(minutes=10)
    queue_order = [event_ids[2], event_ids[1], event_ids[3], event_ids[0]]
    values = {
        event_ids[0]: (anchor + timedelta(seconds=3), anchor),
        event_ids[1]: (anchor + timedelta(seconds=1), anchor + timedelta(seconds=2)),
        event_ids[2]: (anchor + timedelta(seconds=1), anchor + timedelta(seconds=1)),
        event_ids[3]: (anchor + timedelta(seconds=2), anchor),
    }
    async with engine.begin() as connection:
        for event_id, (next_attempt_at, created_at) in values.items():
            await connection.execute(
                update(memory_jobs)
                .where(memory_jobs.c.event_id == event_id)
                .values(next_attempt_at=next_attempt_at, created_at=created_at)
            )

    claimed = await PostgresMemoryJobQueueAdapter(engine).claim_due(
        lease_owner=uuid4(),
        limit=4,
        lease_seconds=120,
        max_attempts=5,
    )

    assert [job.event_id for job in claimed] == queue_order


async def test_claim_filters_future_and_exhausted_jobs(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    due_id = await _schedule(engine, session_id, 1)
    future_id = await _schedule(engine, session_id, 2)
    exhausted_id = await _schedule(engine, session_id, 3)
    async with engine.begin() as connection:
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == future_id)
            .values(next_attempt_at=func.now() + timedelta(minutes=5))
        )
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == exhausted_id)
            .values(attempt_count=5)
        )

    owner = uuid4()
    claimed = await PostgresMemoryJobQueueAdapter(engine).claim_due(
        lease_owner=owner,
        limit=10,
        lease_seconds=120,
        max_attempts=5,
    )

    assert [job.event_id for job in claimed] == [due_id]
    assert claimed[0].attempt_count == 1
    assert claimed[0].reference.user_id == USER_ID
    assert claimed[0].reference.session_id == session_id
    assert claimed[0].reclaimed is False
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    memory_jobs.c.status,
                    memory_jobs.c.lease_owner,
                    memory_jobs.c.lease_token,
                    memory_jobs.c.lease_expires_at,
                ).where(memory_jobs.c.event_id == due_id)
            )
        ).one()
    assert row.status == MemoryJobStatus.PROCESSING.value
    assert row.lease_owner == owner
    assert row.lease_token == claimed[0].lease_token
    assert row.lease_expires_at == claimed[0].lease_expires_at


async def test_skip_locked_prevents_duplicate_claims_between_workers(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    locked_id = await _schedule(engine, session_id, 1)
    available_id = await _schedule(engine, session_id, 2)
    queue = PostgresMemoryJobQueueAdapter(engine)

    async with engine.begin() as locking_connection:
        await locking_connection.execute(
            select(memory_jobs.c.event_id)
            .where(memory_jobs.c.event_id == locked_id)
            .with_for_update()
        )
        async with asyncio.timeout(2):
            claimed = await queue.claim_due(
                lease_owner=uuid4(),
                limit=2,
                lease_seconds=120,
                max_attempts=5,
            )
        assert [job.event_id for job in claimed] == [available_id]

    remaining = await queue.claim_due(
        lease_owner=uuid4(),
        limit=2,
        lease_seconds=120,
        max_attempts=5,
    )
    assert [job.event_id for job in remaining] == [locked_id]
    assert {job.event_id for job in claimed}.isdisjoint(job.event_id for job in remaining)


async def test_two_workers_claim_all_due_jobs_without_overlap(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    event_ids = {await _schedule(engine, session_id, number) for number in range(12)}
    first_owner = uuid4()
    second_owner = uuid4()
    first_queue = PostgresMemoryJobQueueAdapter(engine)
    second_queue = PostgresMemoryJobQueueAdapter(engine)

    first, second = await asyncio.gather(
        first_queue.claim_due(
            lease_owner=first_owner,
            limit=6,
            lease_seconds=120,
            max_attempts=5,
        ),
        second_queue.claim_due(
            lease_owner=second_owner,
            limit=6,
            lease_seconds=120,
            max_attempts=5,
        ),
    )

    first_ids = {job.event_id for job in first}
    second_ids = {job.event_id for job in second}
    assert len(first) == len(second) == 6
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == event_ids
    async with engine.connect() as connection:
        leased = dict(
            (
                await connection.execute(
                    select(memory_jobs.c.event_id, memory_jobs.c.lease_owner).where(
                        memory_jobs.c.event_id.in_(event_ids)
                    )
                )
            )
            .tuples()
            .all()
        )
    assert {leased[event_id] for event_id in first_ids} == {first_owner}
    assert {leased[event_id] for event_id in second_ids} == {second_owner}


async def test_retry_expiry_reclaim_and_complete_are_lease_guarded(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    event_id = await _schedule(engine, session_id, 1)
    queue = PostgresMemoryJobQueueAdapter(engine)
    first = (
        await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )
    )[0]

    with pytest.raises(MemoryJobLeaseLostError):
        await queue.complete(event_id, uuid4(), lifecycle_event_count=1)

    future = datetime.now(UTC) + timedelta(minutes=5)
    await queue.retry(
        event_id,
        first.lease_token,
        next_attempt_at=future,
        error_class="LongTermMemoryTimeoutError",
    )
    assert (
        await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )
        == ()
    )

    async with engine.begin() as connection:
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == event_id)
            .values(next_attempt_at=func.now() - timedelta(seconds=1))
        )
    second = (
        await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )
    )[0]
    assert second.attempt_count == 2
    assert second.reclaimed is False

    async with engine.begin() as connection:
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == event_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
    with pytest.raises(MemoryJobLeaseLostError):
        await queue.complete(event_id, second.lease_token, lifecycle_event_count=1)

    reclaimed = (
        await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )
    )[0]
    assert reclaimed.event_id == event_id
    assert reclaimed.attempt_count == 3
    assert reclaimed.reclaimed is True
    assert reclaimed.lease_token != second.lease_token

    await queue.complete(event_id, reclaimed.lease_token, lifecycle_event_count=2)
    with pytest.raises(MemoryJobLeaseLostError):
        await queue.complete(event_id, reclaimed.lease_token, lifecycle_event_count=2)
    async with engine.connect() as connection:
        completed = (
            (
                await connection.execute(
                    select(memory_jobs).where(memory_jobs.c.event_id == event_id)
                )
            )
            .mappings()
            .one()
        )
    assert completed["status"] == MemoryJobStatus.COMPLETED.value
    assert completed["attempt_count"] == 3
    assert completed["lifecycle_event_count"] == 2
    assert completed["completed_at"] is not None
    assert completed["lease_token"] is None
    assert completed["last_error_class"] is None


async def test_expired_final_attempt_is_dead_without_sixth_delivery(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    event_id = await _schedule(engine, session_id, 1)
    queue = PostgresMemoryJobQueueAdapter(engine)
    claimed = (
        await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=1,
        )
    )[0]
    async with engine.begin() as connection:
        await connection.execute(
            update(memory_jobs)
            .where(memory_jobs.c.event_id == event_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )

    assert (
        await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=1,
        )
        == ()
    )
    async with engine.connect() as connection:
        dead = (
            (
                await connection.execute(
                    select(memory_jobs).where(memory_jobs.c.event_id == event_id)
                )
            )
            .mappings()
            .one()
        )
    assert dead["status"] == MemoryJobStatus.DEAD.value
    assert dead["attempt_count"] == claimed.attempt_count == 1
    assert dead["last_error_class"] == "MemoryJobAttemptsExhaustedError"
    assert dead["dead_at"] is not None
    assert dead["lease_owner"] is None
    assert dead["lease_token"] is None
    assert dead["lease_expires_at"] is None


async def test_claimed_references_remain_isolated_for_same_session_across_users(
    engine: AsyncEngine,
) -> None:
    session_id = f"shared-memory-job-session-{uuid4()}"
    first_user = f"memory-job-user-a-{uuid4()}"
    second_user = f"memory-job-user-b-{uuid4()}"
    try:
        first_id = await _schedule(engine, session_id, 1, user_id=first_user)
        second_id = await _schedule(engine, session_id, 2, user_id=second_user)

        claimed = await PostgresMemoryJobQueueAdapter(engine).claim_due(
            lease_owner=uuid4(),
            limit=2,
            lease_seconds=120,
            max_attempts=5,
        )

        references = {job.event_id: job.reference for job in claimed}
        assert set(references) == {first_id, second_id}
        assert references[first_id].user_id == first_user
        assert references[second_id].user_id == second_user
        assert references[first_id].session_id == references[second_id].session_id == session_id
        assert references[first_id].conversation_id != references[second_id].conversation_id
        assert references[first_id].turn_id != references[second_id].turn_id
        assert references[first_id].boundary_message_id != references[second_id].boundary_message_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.user_id.in_((first_user, second_user)),
                    conversations.c.session_id == session_id,
                )
            )


async def test_dead_list_stats_and_explicit_requeue(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    queue = PostgresMemoryJobQueueAdapter(engine)
    baseline = await queue.stats()
    event_id = await _schedule(engine, session_id, 1)
    claimed = (
        await queue.claim_due(
            lease_owner=uuid4(),
            limit=1,
            lease_seconds=120,
            max_attempts=5,
        )
    )[0]
    await queue.dead_letter(
        event_id,
        claimed.lease_token,
        error_class="LongTermMemoryProtocolError",
    )

    dead_stats = await queue.stats()
    dead_jobs = await queue.list_dead(limit=100)
    dead = next(job for job in dead_jobs if job.event_id == event_id)
    assert dead_stats.dead == baseline.dead + 1
    assert dead_stats.pending == baseline.pending
    assert dead.attempt_count == 1
    assert dead.requeue_count == 0
    assert dead.last_error_class == "LongTermMemoryProtocolError"

    assert await queue.requeue_dead(event_id) is True
    assert await queue.requeue_dead(event_id) is False
    requeued_stats = await queue.stats()
    assert requeued_stats.dead == baseline.dead
    assert requeued_stats.pending == baseline.pending + 1
    assert requeued_stats.oldest_pending_age_seconds is not None
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(memory_jobs).where(memory_jobs.c.event_id == event_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == MemoryJobStatus.PENDING.value
    assert row["attempt_count"] == 0
    assert row["requeue_count"] == 1
    assert row["last_error_class"] is None
    assert row["dead_at"] is None


async def test_purge_is_globally_bounded_and_never_removes_active_jobs(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    event_ids = [await _schedule(engine, session_id, number) for number in range(4)]
    queue = PostgresMemoryJobQueueAdapter(engine)
    claimed = await queue.claim_due(
        lease_owner=uuid4(),
        limit=3,
        lease_seconds=120,
        max_attempts=5,
    )
    assert [job.event_id for job in claimed] == event_ids[:3]
    await queue.complete(claimed[0].event_id, claimed[0].lease_token, lifecycle_event_count=0)
    await queue.dead_letter(
        claimed[1].event_id,
        claimed[1].lease_token,
        error_class="LongTermMemoryProtocolError",
    )

    future = datetime.now(UTC) + timedelta(minutes=1)
    first_purge = await queue.purge_terminal(
        completed_before=future,
        dead_before=future,
        limit=1,
    )
    second_purge = await queue.purge_terminal(
        completed_before=future,
        dead_before=future,
        limit=10,
    )

    assert first_purge.completed + first_purge.dead == 1
    assert second_purge.completed + second_purge.dead == 1
    assert first_purge.completed + second_purge.completed == 1
    assert first_purge.dead + second_purge.dead == 1
    async with engine.connect() as connection:
        remaining = set(
            (
                await connection.execute(
                    select(memory_jobs.c.event_id).where(memory_jobs.c.event_id.in_(event_ids))
                )
            ).scalars()
        )
    assert remaining == {event_ids[2], event_ids[3]}
