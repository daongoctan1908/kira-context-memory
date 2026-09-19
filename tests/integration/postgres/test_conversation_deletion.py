"""Real PostgreSQL acceptance for idempotent conversation and memory erasure."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    normalize_psycopg_dsn,
)
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import conversation_messages, conversations, memory_jobs

pytestmark = pytest.mark.postgres_integration


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


def _migrate() -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _test_url()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="module")
def migrated_database() -> str:
    asyncio.run(asyncio.to_thread(_migrate))
    return _test_url()


@pytest.fixture
async def deletion_runtime(migrated_database: str):
    engine = create_async_engine(migrated_database, pool_pre_ping=True)
    schema_name = f"memory_delete_{uuid4().hex}"
    collection_name = "memories"
    await asyncio.to_thread(
        _initialize_memory_schema_sync,
        normalize_psycopg_dsn(migrated_database),
        schema_name,
        collection_name,
        "test-embedding-model",
        3,
    )
    store = PostgresConversationStoreAdapter(
        engine,
        memory_enabled=True,
        memory_schema=schema_name,
        memory_collection=collection_name,
    )
    await store.validate_schema()
    try:
        yield engine, store, schema_name
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            )
        await engine.dispose()


async def _append_turn(
    store: PostgresConversationStoreAdapter,
    *,
    user_id: str,
    session_id: str,
    schedule_memory: bool,
):
    now = datetime.now(UTC)
    turn_id = uuid4().hex
    return await store.append_turn(
        user_id,
        ConversationMessage(session_id, turn_id, ConversationRole.USER, "question", now),
        ConversationMessage(
            session_id,
            turn_id,
            ConversationRole.ASSISTANT,
            "answer",
            now + timedelta(milliseconds=1),
        ),
        schedule_memory=schedule_memory,
    )


async def _insert_memory(
    engine: AsyncEngine,
    schema_name: str,
    *,
    memory_id,
    user_id: str,
    conversation_id,
) -> None:
    payload = json.dumps(
        {
            "data": "durable fact",
            "text_lemmatized": "durable fact",
            "user_id": user_id,
            "conversation_id": str(conversation_id),
        }
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f'INSERT INTO "{schema_name}".memories (id, vector, payload) '
                "VALUES (:memory_id, CAST(:vector AS vector), CAST(:payload AS jsonb))"
            ),
            {"memory_id": memory_id, "vector": "[1,0,0]", "payload": payload},
        )


async def test_delete_removes_every_owned_row_without_list_limit_and_is_idempotent(
    deletion_runtime,
) -> None:
    engine, store, schema_name = deletion_runtime
    user_id = f"delete-user-{uuid4().hex}"
    other_user = f"other-user-{uuid4().hex}"
    session_id = f"delete-session-{uuid4().hex}"
    other_session = f"other-session-{uuid4().hex}"
    target = await _append_turn(
        store,
        user_id=user_id,
        session_id=session_id,
        schedule_memory=True,
    )
    other = await _append_turn(
        store,
        user_id=other_user,
        session_id=other_session,
        schedule_memory=False,
    )
    target_ids = [uuid4() for _ in range(25)]
    other_id = uuid4()
    for memory_id in target_ids:
        await _insert_memory(
            engine,
            schema_name,
            memory_id=memory_id,
            user_id=user_id,
            conversation_id=target.reference.conversation_id,
        )
    await _insert_memory(
        engine,
        schema_name,
        memory_id=other_id,
        user_id=other_user,
        conversation_id=other.reference.conversation_id,
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f'INSERT INTO "{schema_name}".memories_formation_receipts '
                "(event_id, user_id, conversation_id, result, memory_count) "
                "VALUES (:event_id, :user_id, :conversation_id, '[]'::jsonb, 0)"
            ),
            {
                "event_id": uuid4(),
                "user_id": user_id,
                "conversation_id": target.reference.conversation_id,
            },
        )
        entities = [
            (
                uuid4(),
                {
                    "user_id": user_id,
                    "linked_memory_ids": [str(target_ids[0]), str(other_id)],
                },
            ),
            (
                uuid4(),
                {"user_id": user_id, "linked_memory_ids": [str(target_ids[1])]},
            ),
            (
                uuid4(),
                {"user_id": other_user, "linked_memory_ids": [str(other_id)]},
            ),
        ]
        for entity_id, payload in entities:
            await connection.execute(
                text(
                    f'INSERT INTO "{schema_name}".memories_entities (id, vector, payload) '
                    "VALUES (:entity_id, CAST(:vector AS vector), CAST(:payload AS jsonb))"
                ),
                {
                    "entity_id": entity_id,
                    "vector": "[1,0,0]",
                    "payload": json.dumps(payload),
                },
            )

    assert not await store.mark_deletion_pending(other_user, session_id)
    assert not await store.purge_deletion_pending(other_user, session_id)
    assert await store.mark_deletion_pending(user_id, session_id)
    assert await store.purge_deletion_pending(user_id, session_id)
    assert not await store.mark_deletion_pending(user_id, session_id)
    assert not await store.purge_deletion_pending(user_id, session_id)

    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count()).select_from(conversations).where(
                    conversations.c.conversation_id == target.reference.conversation_id
                )
            )
            == 0
        )
        assert (
            await connection.scalar(
                select(func.count()).select_from(conversation_messages).where(
                    conversation_messages.c.conversation_id == target.reference.conversation_id
                )
            )
            == 0
        )
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(memory_jobs.join(conversation_messages))
                .where(conversation_messages.c.conversation_id == target.reference.conversation_id)
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".memories '
                    "WHERE payload->>'conversation_id' = :conversation_id"
                ),
                {"conversation_id": str(target.reference.conversation_id)},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    f'SELECT count(*) FROM "{schema_name}".memories_formation_receipts '
                    "WHERE conversation_id = :conversation_id"
                ),
                {"conversation_id": target.reference.conversation_id},
            )
            == 0
        )
        remaining_entities = (
            await connection.execute(
                text(f'SELECT payload FROM "{schema_name}".memories_entities ORDER BY id')
            )
        ).scalars().all()
        assert remaining_entities == [
            {"user_id": other_user, "linked_memory_ids": [str(other_id)]}
        ]
        assert (
            await connection.scalar(
                select(func.count()).select_from(conversations).where(
                    conversations.c.conversation_id == other.reference.conversation_id
                )
            )
            == 1
        )
        assert (
            await connection.scalar(
                text(f'SELECT count(*) FROM "{schema_name}".memories WHERE id = :memory_id'),
                {"memory_id": other_id},
            )
            == 1
        )


async def test_cancelled_delete_rolls_back_and_retry_finishes_pending_conversation(
    deletion_runtime,
) -> None:
    engine, store, schema_name = deletion_runtime
    user_id = f"retry-user-{uuid4().hex}"
    session_id = f"retry-session-{uuid4().hex}"
    target = await _append_turn(
        store,
        user_id=user_id,
        session_id=session_id,
        schedule_memory=False,
    )
    memory_id = uuid4()
    await _insert_memory(
        engine,
        schema_name,
        memory_id=memory_id,
        user_id=user_id,
        conversation_id=target.reference.conversation_id,
    )
    assert await store.mark_deletion_pending(user_id, session_id)

    async with engine.begin() as locking_connection:
        await locking_connection.execute(
            text(f'LOCK TABLE "{schema_name}".memories IN ACCESS EXCLUSIVE MODE')
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                store.purge_deletion_pending(user_id, session_id),
                timeout=0.05,
            )

    async with engine.connect() as connection:
        status = await connection.scalar(
            select(conversations.c.status).where(
                conversations.c.conversation_id == target.reference.conversation_id
            )
        )
        count = await connection.scalar(
            text(f'SELECT count(*) FROM "{schema_name}".memories WHERE id = :memory_id'),
            {"memory_id": memory_id},
        )
    assert status == "deletion_pending"
    assert count == 1

    assert await store.purge_deletion_pending(user_id, session_id)
    assert not await store.purge_deletion_pending(user_id, session_id)


async def test_operator_purge_pending_is_bounded_and_leaves_active_rows(
    deletion_runtime,
) -> None:
    engine, store, _schema_name = deletion_runtime
    while await store.purge_pending_conversations(limit=100):
        pass
    user_id = f"batch-user-{uuid4().hex}"
    pending_sessions = [f"pending-{uuid4().hex}" for _ in range(2)]
    active_session = f"active-{uuid4().hex}"
    for session_id in (*pending_sessions, active_session):
        await _append_turn(
            store,
            user_id=user_id,
            session_id=session_id,
            schedule_memory=False,
        )
    for session_id in pending_sessions:
        assert await store.mark_deletion_pending(user_id, session_id)

    assert await store.purge_pending_conversations(limit=1) == 1
    assert await store.purge_pending_conversations(limit=1) == 1
    assert await store.purge_pending_conversations(limit=1) == 0
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(conversations.c.session_id, conversations.c.status).where(
                    conversations.c.user_id == user_id
                )
            )
        ).tuples().all()
    assert rows == [(active_session, "active")]
