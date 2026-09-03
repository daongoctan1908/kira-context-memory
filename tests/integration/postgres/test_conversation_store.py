import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.errors.conversation import ConversationStoreProtocolError
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    conversation_messages,
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
async def session_id(engine: AsyncEngine):
    value = f"pg-test-{uuid4()}"
    try:
        yield value
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(conversations.c.session_id == value)
            )


def _message(
    session_id: str,
    turn_id: str,
    role: ConversationRole,
    content: str,
    offset: int,
) -> ConversationMessage:
    return ConversationMessage(
        session_id=session_id,
        turn_id=turn_id,
        role=role,
        content=content,
        timestamp=datetime(2026, 9, 3, 9, 0, tzinfo=UTC) + timedelta(seconds=offset),
    )


async def _append(
    adapter: PostgresConversationStoreAdapter,
    session_id: str,
    turn_number: int,
    *,
    turn_id: str | None = None,
) -> bool:
    resolved_turn_id = turn_id or f"{session_id}-turn-{turn_number}"
    return await adapter.append_turn(
        _message(
            session_id,
            resolved_turn_id,
            ConversationRole.USER,
            f"Câu hỏi tiếng Việt {turn_number}",
            turn_number * 2,
        ),
        _message(
            session_id,
            resolved_turn_id,
            ConversationRole.ASSISTANT,
            f"Trả lời tiếng Việt {turn_number}",
            turn_number * 2 + 1,
        ),
    )


async def test_migration_revision_and_recent_index_exist(engine: AsyncEngine) -> None:
    adapter = PostgresConversationStoreAdapter(engine)

    await adapter.validate_schema()
    async with engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        index_definition = await connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'ix_messages_conversation_recent'"
            )
        )

    assert revision == EXPECTED_SCHEMA_REVISION
    assert index_definition is not None
    assert "conversation_id" in index_definition
    assert "turn_sequence DESC" in index_definition
    assert "message_index DESC" in index_definition


async def test_full_history_is_retained_while_recent_read_is_bounded(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)

    for turn_number in range(1, 7):
        assert await _append(adapter, session_id, turn_number) is True

    recent = await adapter.read_recent(session_id, 10)
    async with engine.connect() as connection:
        persisted_count = await connection.scalar(
            select(text("count(*)"))
            .select_from(conversation_messages.join(conversations))
            .where(conversations.c.session_id == session_id)
        )
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        query_plan = (
            (
                await connection.execute(
                    text(
                        "EXPLAIN SELECT m.* FROM conversation_messages AS m "
                        "JOIN conversations AS c ON c.conversation_id = m.conversation_id "
                        "WHERE c.session_id = :session_id "
                        "ORDER BY m.turn_sequence DESC, m.message_index DESC LIMIT 10"
                    ),
                    {"session_id": session_id},
                )
            )
            .scalars()
            .all()
        )

    assert persisted_count == 12
    assert len(recent) == 10
    assert recent[0].content == "Câu hỏi tiếng Việt 2"
    assert recent[-1].content == "Trả lời tiếng Việt 6"
    assert any("ix_messages_conversation_recent" in line for line in query_plan)
    assert [item.role for item in recent] == [
        ConversationRole.USER,
        ConversationRole.ASSISTANT,
    ] * 5


async def test_duplicate_is_idempotent_and_collision_is_rejected(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    turn_id = f"global-{uuid4()}"

    assert await _append(adapter, session_id, 1, turn_id=turn_id) is True
    assert await _append(adapter, session_id, 1, turn_id=turn_id) is False

    with pytest.raises(ConversationStoreProtocolError):
        await adapter.append_turn(
            _message(session_id, turn_id, ConversationRole.USER, "different", 2),
            _message(session_id, turn_id, ConversationRole.ASSISTANT, "answer", 3),
        )

    assert len(await adapter.read_recent(session_id, 10)) == 2


async def test_concurrent_append_preserves_pair_order_and_session_isolation(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    adapter = PostgresConversationStoreAdapter(engine)
    other_session = f"pg-test-{uuid4()}"
    try:
        results = await asyncio.gather(
            *(_append(adapter, session_id, turn_number) for turn_number in range(1, 6)),
            _append(adapter, other_session, 1),
        )

        assert all(results)
        recent = await adapter.read_recent(session_id, 10)
        other_recent = await adapter.read_recent(other_session, 10)
        assert len(recent) == 10
        assert len(other_recent) == 2
        for index in range(0, len(recent), 2):
            assert recent[index].role is ConversationRole.USER
            assert recent[index + 1].role is ConversationRole.ASSISTANT
            assert recent[index].turn_id == recent[index + 1].turn_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(conversations).where(conversations.c.session_id == other_session)
            )


async def test_malformed_persisted_schema_maps_to_protocol_error(
    engine: AsyncEngine,
    session_id: str,
) -> None:
    conversation_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(conversations).values(
                conversation_id=conversation_id,
                session_id=session_id,
                next_turn_sequence=2,
            )
        )
        await connection.execute(
            insert(conversation_messages),
            [
                {
                    "conversation_id": conversation_id,
                    "turn_id": f"bad-{uuid4()}",
                    "turn_sequence": 1,
                    "message_index": index,
                    "role": role,
                    "content": "invalid schema payload",
                    "message_timestamp": datetime.now(UTC),
                    "schema_version": 99,
                }
                for index, role in enumerate(("user", "assistant"))
            ],
        )

    with pytest.raises(ConversationStoreProtocolError):
        await PostgresConversationStoreAdapter(engine).read_recent(session_id, 10)
