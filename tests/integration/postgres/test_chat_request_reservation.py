"""PostgreSQL acceptance tests for fenced chat request reservations."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.errors.conversation import (
    ChatRequestConflictError,
    ChatRequestLeaseLostError,
    ConversationSourceUnavailableError,
    ConversationStoreProtocolError,
)
from app.domain.models.conversation import (
    ChatRequestReservationOutcome,
    ChatRequestStatus,
    ConversationMessage,
    ConversationRole,
)
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    chat_requests,
    conversation_messages,
    conversations,
    memory_jobs,
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


async def test_completion_atomically_persists_turn_job_and_replays_without_new_owner(
    managed_conversation,
    engine: AsyncEngine,
) -> None:
    adapter, user_id, created = managed_conversation
    client_message_id = uuid4()
    now = datetime.now(UTC)
    reservation = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        client_message_id,
        sha256(b"hello").digest(),
        now=now,
        lease_seconds=120,
    )
    assert reservation is not None
    user = ConversationMessage(
        created.session_id,
        reservation.turn_id,
        ConversationRole.USER,
        "hello",
        now,
    )
    assistant = ConversationMessage(
        created.session_id,
        reservation.turn_id,
        ConversationRole.ASSISTANT,
        "xin chao",
        now + timedelta(seconds=1),
    )

    completed = await adapter.complete_chat_request(
        user_id,
        reservation,
        user,
        assistant,
        completed_at=now + timedelta(seconds=2),
        schedule_memory=True,
    )
    assert completed.inserted
    assert completed.memory_job_event_id is not None

    replay_reservation = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        client_message_id,
        sha256(b"hello").digest(),
        now=now + timedelta(seconds=3),
        lease_seconds=120,
    )
    assert replay_reservation is not None
    assert replay_reservation.outcome is ChatRequestReservationOutcome.COMPLETED
    replay = await adapter.read_completed_chat_request(
        user_id,
        created.session_id,
        replay_reservation,
    )
    assert replay is not None
    assert [message.content for message in replay] == ["hello", "xin chao"]

    async with engine.connect() as connection:
        request_row = (
            (
                await connection.execute(
                    select(chat_requests).where(
                        chat_requests.c.request_id == reservation.request_id
                    )
                )
            )
            .mappings()
            .one()
        )
        message_count = await connection.scalar(
            select(func.count())
            .select_from(conversation_messages)
            .where(conversation_messages.c.turn_id == reservation.turn_id)
        )
        job_count = await connection.scalar(
            select(func.count())
            .select_from(memory_jobs)
            .where(memory_jobs.c.boundary_message_id == completed.reference.boundary_message_id)
        )
    assert request_row["status"] == "completed"
    assert request_row["lease_token"] is None
    assert message_count == 2
    assert job_count == 1


@pytest.mark.parametrize("reclaim", [False, True])
async def test_completion_and_legacy_reads_use_first_request_time(
    managed_conversation,
    engine: AsyncEngine,
    reclaim: bool,
) -> None:
    adapter, user_id, created = managed_conversation
    attempt_time = datetime.now(UTC)
    source_time = attempt_time - timedelta(minutes=10 if reclaim else 1)
    client_message_id = uuid4()
    content = "remember the original source time"
    digest = sha256(content.encode()).digest()
    reservation = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        client_message_id,
        digest,
        now=source_time,
        lease_seconds=60 if reclaim else 180,
    )
    assert reservation is not None
    if reclaim:
        reservation = await adapter.reserve_chat_request(
            user_id,
            created.session_id,
            client_message_id,
            digest,
            now=attempt_time,
            lease_seconds=120,
        )
        assert reservation is not None
        assert reservation.outcome is ChatRequestReservationOutcome.RECLAIMED
    user = ConversationMessage(
        created.session_id,
        reservation.turn_id,
        ConversationRole.USER,
        content,
        attempt_time,
    )
    assistant = ConversationMessage(
        created.session_id,
        reservation.turn_id,
        ConversationRole.ASSISTANT,
        "acknowledged",
        attempt_time + timedelta(seconds=1),
    )
    completed = await adapter.complete_chat_request(
        user_id,
        reservation,
        user,
        assistant,
        completed_at=attempt_time + timedelta(seconds=2),
        schedule_memory=True,
    )
    assert completed.memory_job_event_id is not None
    async with engine.connect() as connection:
        stored_timestamps = (
            (
                await connection.execute(
                    select(conversation_messages.c.message_timestamp)
                    .where(conversation_messages.c.turn_id == reservation.turn_id)
                    .order_by(conversation_messages.c.message_index)
                )
            )
            .scalars()
            .all()
        )
    assert stored_timestamps == [source_time, assistant.timestamp]

    # Pre-change queued turns stored the attempt's clock. Reads recover the original
    # request time without rewriting that historical row or the assistant timestamp.
    async with engine.begin() as connection:
        await connection.execute(
            update(conversation_messages)
            .where(
                conversation_messages.c.turn_id == reservation.turn_id,
                conversation_messages.c.role == ConversationRole.USER.value,
            )
            .values(message_timestamp=attempt_time)
        )
    recent = await adapter.read_recent(user_id, created.session_id, 10)
    boundary = await adapter.read_through_boundary(
        user_id,
        created.conversation_id,
        completed.reference.boundary_message_id,
        10,
    )
    assert [message.timestamp for message in recent] == [source_time, assistant.timestamp]
    assert boundary == recent
    assert await adapter.read_recent("other-user", created.session_id, 10) == ()
    with pytest.raises(ConversationSourceUnavailableError):
        await adapter.read_through_boundary(
            "other-user", created.conversation_id, completed.reference.boundary_message_id, 10
        )
    async with engine.connect() as connection:
        stored_user_timestamp = await connection.scalar(
            select(conversation_messages.c.message_timestamp).where(
                conversation_messages.c.turn_id == reservation.turn_id,
                conversation_messages.c.role == ConversationRole.USER.value,
            )
        )
        original_request_time = await connection.scalar(
            select(chat_requests.c.created_at).where(
                chat_requests.c.request_id == reservation.request_id
            )
        )
    assert stored_user_timestamp == attempt_time
    assert original_request_time == source_time


async def test_legacy_turn_without_reservation_keeps_stored_source_time(
    managed_conversation,
    engine: AsyncEngine,
) -> None:
    adapter, user_id, created = managed_conversation
    source_time = datetime.now(UTC) - timedelta(days=1)
    turn_id = uuid4().hex
    user = ConversationMessage(
        created.session_id, turn_id, ConversationRole.USER, "legacy /chat source", source_time
    )
    assistant = ConversationMessage(
        created.session_id,
        turn_id,
        ConversationRole.ASSISTANT,
        "legacy answer",
        source_time + timedelta(seconds=1),
    )
    completed = await adapter.append_turn(user_id, user, assistant, schedule_memory=True)

    assert await adapter.read_recent(user_id, created.session_id, 10) == (user, assistant)
    assert await adapter.read_through_boundary(
        user_id, created.conversation_id, completed.reference.boundary_message_id, 10
    ) == (user, assistant)
    async with engine.connect() as connection:
        request_count = await connection.scalar(
            select(func.count())
            .select_from(chat_requests)
            .where(chat_requests.c.turn_id == turn_id)
        )
    assert request_count == 0


async def test_stale_completion_is_fenced_and_writes_nothing(
    managed_conversation,
    engine: AsyncEngine,
) -> None:
    adapter, user_id, created = managed_conversation
    client_message_id = uuid4()
    old_now = datetime.now(UTC) - timedelta(minutes=10)
    digest = sha256(b"stale").digest()
    stale = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        client_message_id,
        digest,
        now=old_now,
        lease_seconds=60,
    )
    current = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        client_message_id,
        digest,
        now=datetime.now(UTC),
        lease_seconds=120,
    )
    assert stale is not None and current is not None
    timestamp = datetime.now(UTC)
    user = ConversationMessage(
        created.session_id,
        stale.turn_id,
        ConversationRole.USER,
        "stale",
        timestamp,
    )
    assistant = ConversationMessage(
        created.session_id,
        stale.turn_id,
        ConversationRole.ASSISTANT,
        "must not persist",
        timestamp,
    )

    with pytest.raises(ChatRequestLeaseLostError):
        await adapter.complete_chat_request(
            user_id,
            stale,
            user,
            assistant,
            completed_at=timestamp,
        )
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count())
            .select_from(conversation_messages)
            .where(conversation_messages.c.turn_id == stale.turn_id)
        )
    assert count == 0


async def test_completion_rolls_back_when_memory_job_scheduling_fails(
    managed_conversation,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, user_id, created = managed_conversation
    now = datetime.now(UTC)
    reservation = await adapter.reserve_chat_request(
        user_id,
        created.session_id,
        uuid4(),
        sha256(b"rollback").digest(),
        now=now,
        lease_seconds=120,
    )
    assert reservation is not None

    async def fail_schedule(*_args, **_kwargs):
        raise ConversationStoreProtocolError

    monkeypatch.setattr(adapter, "_schedule_memory_job", fail_schedule)
    with pytest.raises(ConversationStoreProtocolError):
        await adapter.complete_chat_request(
            user_id,
            reservation,
            ConversationMessage(
                created.session_id,
                reservation.turn_id,
                ConversationRole.USER,
                "rollback",
                now,
            ),
            ConversationMessage(
                created.session_id,
                reservation.turn_id,
                ConversationRole.ASSISTANT,
                "not committed",
                now,
            ),
            completed_at=now + timedelta(seconds=1),
            schedule_memory=True,
        )
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(chat_requests.c.status).where(
                    chat_requests.c.request_id == reservation.request_id
                )
            )
        ).one()
        count = await connection.scalar(
            select(func.count())
            .select_from(conversation_messages)
            .where(conversation_messages.c.turn_id == reservation.turn_id)
        )
    assert row.status == "processing"
    assert count == 0
