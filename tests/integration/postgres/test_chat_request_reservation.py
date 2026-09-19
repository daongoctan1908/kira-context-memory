"""PostgreSQL acceptance tests for fenced chat request reservations."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.errors.conversation import ChatRequestConflictError
from app.domain.models.conversation import (
    ChatRequestReservationOutcome,
    ChatRequestStatus,
)
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    chat_requests,
    conversations,
)

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


@pytest.fixture
async def managed_conversation(engine: AsyncEngine):
    adapter = PostgresConversationStoreAdapter(engine)
    await adapter.validate_schema()
    user_id = f"reservation-user-{uuid4()}"
    created = await adapter.create_conversation(user_id, title="Reservation test")
    try:
        yield adapter, user_id, created
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(
                    conversations.c.conversation_id == created.conversation_id
                )
            )


async def test_migration_has_partial_processing_indexes(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        definitions = (
            (
                await connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'chat_requests'"
                    )
                )
            )
            .mappings()
            .all()
        )
    indexes = {row["indexname"]: row["indexdef"] for row in definitions}
    assert revision == EXPECTED_SCHEMA_REVISION
    assert "UNIQUE" in indexes["uq_chat_requests_conversation_processing"]
    assert "WHERE (status = 'processing'" in indexes["uq_chat_requests_conversation_processing"]
    assert "lease_expires_at" in indexes["ix_chat_requests_processing_lease"]


async def test_concurrent_duplicate_has_one_owner_and_detects_content_conflict(
    managed_conversation,
) -> None:
    adapter, user_id, created = managed_conversation
    client_message_id = uuid4()
    now = datetime.now(UTC)
    digest = sha256(b"hello").digest()

    reservations = await asyncio.gather(
        *(
            adapter.reserve_chat_request(
                user_id,
                created.session_id,
                client_message_id,
                digest,
                now=now,
                lease_seconds=120,
            )
            for _ in range(2)
        )
    )
    assert {item.outcome for item in reservations if item is not None} == {
        ChatRequestReservationOutcome.ACQUIRED,
        ChatRequestReservationOutcome.IN_PROGRESS,
    }
    assert sum(item.lease_token is not None for item in reservations if item is not None) == 1

    with pytest.raises(ChatRequestConflictError):
        await adapter.reserve_chat_request(
            user_id,
            created.session_id,
            client_message_id,
            sha256(b"different").digest(),
            now=now,
            lease_seconds=120,
        )
    assert (
        await adapter.reserve_chat_request(
            "other-user",
            created.session_id,
            uuid4(),
            digest,
            now=now,
            lease_seconds=120,
        )
        is None
    )


async def test_expired_attempt_reclaims_and_stale_owner_cannot_release(
    managed_conversation,
    engine: AsyncEngine,
) -> None:
    adapter, user_id, created = managed_conversation
    old_now = datetime.now(UTC) - timedelta(minutes=10)
    client_message_id = uuid4()
    digest = sha256(b"reclaim me").digest()
    first = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        client_message_id,
        digest,
        now=old_now,
        lease_seconds=60,
    )
    assert first is not None and first.lease_token is not None

    reclaimed = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        client_message_id,
        digest,
        now=datetime.now(UTC),
        lease_seconds=120,
    )
    assert reclaimed is not None and reclaimed.lease_token is not None
    assert reclaimed.outcome is ChatRequestReservationOutcome.RECLAIMED
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_token != first.lease_token

    assert not await adapter.abandon_chat_request(
        first.request_id,
        first.lease_token,
        status=ChatRequestStatus.FAILED,
        now=datetime.now(UTC),
    )
    assert await adapter.abandon_chat_request(
        reclaimed.request_id,
        reclaimed.lease_token,
        status=ChatRequestStatus.CANCELLED,
        now=datetime.now(UTC),
    )
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(chat_requests).where(chat_requests.c.request_id == reclaimed.request_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "cancelled"
    assert row["lease_token"] is None

    replacement = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        uuid4(),
        sha256(b"next message").digest(),
        now=datetime.now(UTC),
        lease_seconds=120,
    )
    assert replacement is not None
    assert replacement.outcome is ChatRequestReservationOutcome.ACQUIRED
