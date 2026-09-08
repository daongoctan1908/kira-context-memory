"""PostgreSQL migration and constraint tests for the durable memory-job queue."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    conversations,
    memory_jobs,
)

pytestmark = pytest.mark.postgres_integration
USER_ID = "memory-job-schema-user"


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
async def completed_boundary(engine: AsyncEngine):
    session_id = f"memory-job-schema-{uuid4()}"
    turn_id = str(uuid4())
    timestamp = datetime.now(UTC)
    adapter = PostgresConversationStoreAdapter(engine)
    result = await adapter.append_turn(
        USER_ID,
        ConversationMessage(
            session_id=session_id,
            turn_id=turn_id,
            role=ConversationRole.USER,
            content="Remember my explicitly confirmed reporting preference.",
            timestamp=timestamp,
        ),
        ConversationMessage(
            session_id=session_id,
            turn_id=turn_id,
            role=ConversationRole.ASSISTANT,
            content="Confirmed.",
            timestamp=timestamp + timedelta(milliseconds=1),
        ),
    )
    try:
        yield result.reference
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(conversations.c.session_id == session_id)
            )


async def test_migration_creates_expected_revision_columns_and_partial_indexes(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'memory_jobs' "
                        "ORDER BY ordinal_position"
                    )
                )
            )
            .scalars()
            .all()
        )
        index_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = current_schema() AND tablename = 'memory_jobs'"
                    )
                )
            )
            .tuples()
            .all()
        )

    assert revision == EXPECTED_SCHEMA_REVISION
    assert columns == list(memory_jobs.c.keys())
    indexes = dict(index_rows)
    for index_name, status in {
        "ix_memory_jobs_pending_due": "pending",
        "ix_memory_jobs_processing_lease": "processing",
        "ix_memory_jobs_completed_cleanup": "completed",
        "ix_memory_jobs_dead_cleanup": "dead",
    }.items():
        assert index_name in indexes
        assert " WHERE " in indexes[index_name]
        assert status in indexes[index_name]


async def test_defaults_create_pending_reference_only_job(
    engine: AsyncEngine,
    completed_boundary,
) -> None:
    event_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(memory_jobs).values(
                event_id=event_id,
                boundary_message_id=completed_boundary.boundary_message_id,
            )
        )
        row = (
            (
                await connection.execute(
                    select(memory_jobs).where(memory_jobs.c.event_id == event_id)
                )
            )
            .mappings()
            .one()
        )

    assert row["status"] == "pending"
    assert row["schema_version"] == 1
    assert row["attempt_count"] == 0
    assert row["requeue_count"] == 0
    assert row["next_attempt_at"].tzinfo is not None
    assert row["lease_owner"] is None
    assert row["lease_token"] is None
    assert row["lease_expires_at"] is None
    assert row["completed_at"] is None
    assert row["dead_at"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"status": "unknown"},
        {"attempt_count": -1},
        {"requeue_count": -1},
        {"lifecycle_event_count": -1},
        {"last_error_class": "contains space"},
        {"status": "processing"},
        {"lease_owner": uuid4()},
        {"status": "completed"},
        {"status": "completed", "completed_at": datetime.now(UTC), "lifecycle_event_count": 0},
        {"status": "dead", "dead_at": datetime.now(UTC)},
        {
            "status": "dead",
            "attempt_count": 1,
            "dead_at": datetime(2000, 1, 1, tzinfo=UTC),
            "last_error_class": "LongTermMemoryProtocolError",
        },
    ],
)
async def test_database_rejects_invalid_job_state(
    engine: AsyncEngine,
    completed_boundary,
    overrides: dict[str, object],
) -> None:
    values = {
        "event_id": uuid4(),
        "boundary_message_id": completed_boundary.boundary_message_id,
        **overrides,
    }
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(insert(memory_jobs).values(**values))


@pytest.mark.parametrize("status", ["processing", "completed", "dead"])
async def test_database_accepts_each_nonpending_lifecycle_state(
    engine: AsyncEngine,
    completed_boundary,
    status: str,
) -> None:
    terminal_time = datetime.now(UTC) + timedelta(seconds=1)
    status_values: dict[str, object] = {"status": status, "attempt_count": 1}
    if status == "processing":
        status_values.update(
            lease_owner=uuid4(),
            lease_token=uuid4(),
            lease_expires_at=terminal_time + timedelta(minutes=2),
        )
    elif status == "completed":
        status_values.update(completed_at=terminal_time, lifecycle_event_count=0)
    else:
        status_values.update(
            attempt_count=5,
            dead_at=terminal_time,
            last_error_class="LongTermMemoryProtocolError",
        )

    async with engine.begin() as connection:
        await connection.execute(
            insert(memory_jobs).values(
                event_id=uuid4(),
                boundary_message_id=completed_boundary.boundary_message_id,
                **status_values,
            )
        )


async def test_boundary_is_unique_and_cascades_with_conversation(
    engine: AsyncEngine,
    completed_boundary,
) -> None:
    first_event_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(memory_jobs).values(
                event_id=first_event_id,
                boundary_message_id=completed_boundary.boundary_message_id,
            )
        )

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                insert(memory_jobs).values(
                    event_id=uuid4(),
                    boundary_message_id=completed_boundary.boundary_message_id,
                )
            )

    async with engine.begin() as connection:
        await connection.execute(
            delete(conversations).where(
                conversations.c.conversation_id == completed_boundary.conversation_id
            )
        )
    async with engine.connect() as connection:
        remaining = await connection.scalar(
            select(memory_jobs.c.event_id).where(memory_jobs.c.event_id == first_event_id)
        )
    assert remaining is None
