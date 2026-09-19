"""Real PostgreSQL gates for active-owner retrieval and late formation writes."""

import asyncio
import os
from functools import partial
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from mem0.vector_stores.pgvector import InactiveMemoryOwnerError, PGVector
from psycopg import sql

from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    normalize_psycopg_dsn,
)

pytestmark = pytest.mark.postgres_integration


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return value


def _test_dsn() -> str:
    return normalize_psycopg_dsn(_test_url())


def _migrate_application_schema() -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _test_url()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _vector_store(dsn: str, schema_name: str, *, fenced: bool) -> PGVector:
    return PGVector(
        dbname="unused",
        collection_name="memories",
        embedding_model_dims=3,
        user=None,
        password=None,
        host=None,
        port=None,
        diskann=False,
        hnsw=True,
        minconn=1,
        maxconn=2,
        connection_string=dsn,
        schema_name=schema_name,
        auto_create=False,
        enforce_active_conversation_ownership=fenced,
        conversation_schema_name="public",
        conversation_table_name="conversations",
    )


def _insert_conversation(
    connection: psycopg.Connection,
    conversation_id: UUID,
    user_id: str,
    status: str,
) -> None:
    connection.execute(
        "INSERT INTO public.conversations "
        "(conversation_id, user_id, session_id, status) VALUES (%s, %s, %s, %s)",
        (conversation_id, user_id, uuid4().hex, status),
    )


def _formation_call(store: PGVector, event_id: str, conversation_id: str, memory_id: str):
    result = [{"id": memory_id, "memory": "durable active fact", "event": "ADD"}]
    return store.insert_with_formation_receipt(
        [[1.0, 0.0, 0.0]],
        [
            {
                "user_id": "owner-user",
                "formation_event_id": event_id,
                "conversation_id": conversation_id,
                "data": "durable active fact",
                "text_lemmatized": "durable active fact",
            }
        ],
        [memory_id],
        event_id=event_id,
        user_id="owner-user",
        conversation_id=conversation_id,
        result=result,
    )


async def test_search_excludes_pending_missing_and_cross_user_owners() -> None:
    await asyncio.to_thread(_migrate_application_schema)
    dsn = _test_dsn()
    schema_name = f"memory_owner_search_{uuid4().hex}"
    active_id, pending_id, other_id = uuid4(), uuid4(), uuid4()
    writer = reader = None
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        with psycopg.connect(dsn) as connection:
            _insert_conversation(connection, active_id, "owner-user", "active")
            _insert_conversation(connection, pending_id, "owner-user", "deletion_pending")
            _insert_conversation(connection, other_id, "other-user", "active")

        writer = _vector_store(dsn, schema_name, fenced=False)
        reader = _vector_store(dsn, schema_name, fenced=True)
        payloads = [
            {
                "user_id": "owner-user",
                "conversation_id": str(active_id),
                "data": "active",
            },
            {
                "user_id": "owner-user",
                "conversation_id": str(pending_id),
                "data": "pending",
            },
            {
                "user_id": "owner-user",
                "conversation_id": str(uuid4()),
                "data": "missing",
            },
            {
                "user_id": "owner-user",
                "conversation_id": str(other_id),
                "data": "cross-user",
            },
        ]
        writer.insert(
            [[1.0, 0.0, 0.0]] * len(payloads),
            payloads=payloads,
            ids=[str(uuid4()) for _ in payloads],
        )

        results = reader.search(
            "active",
            [1.0, 0.0, 0.0],
            top_k=10,
            filters={"user_id": "owner-user"},
        )

        assert [row.payload["data"] for row in results] == ["active"]
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE conversation_id = ANY(%s::uuid[])",
                ([str(active_id), str(pending_id), str(other_id)],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_delete_lock_wins_after_timeout_and_late_persist_cannot_recreate_memory() -> None:
    await asyncio.to_thread(_migrate_application_schema)
    dsn = _test_dsn()
    schema_name = f"memory_owner_late_{uuid4().hex}"
    conversation_id = uuid4()
    event_id = str(uuid4())
    memory_id = str(uuid4())
    store = None
    delete_connection = None
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        with psycopg.connect(dsn) as connection:
            _insert_conversation(connection, conversation_id, "owner-user", "active")

        store = _vector_store(dsn, schema_name, fenced=True)
        delete_connection = psycopg.connect(dsn)
        delete_connection.execute(
            "SELECT 1 FROM public.conversations WHERE conversation_id = %s FOR UPDATE",
            (conversation_id,),
        )
        delete_connection.execute(
            "UPDATE public.conversations SET status = 'deletion_pending' "
            "WHERE conversation_id = %s",
            (conversation_id,),
        )

        loop = asyncio.get_running_loop()
        late_persist = loop.run_in_executor(
            None,
            partial(
                _formation_call,
                store,
                event_id,
                str(conversation_id),
                memory_id,
            ),
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(late_persist), timeout=0.05)

        delete_connection.commit()
        with pytest.raises(InactiveMemoryOwnerError):
            await late_persist

        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                sql.SQL(
                    "SELECT "
                    "(SELECT count(*) FROM {}.memories WHERE id = %s), "
                    "(SELECT count(*) FROM {}.memories_formation_receipts WHERE event_id = %s)"
                ).format(sql.Identifier(schema_name), sql.Identifier(schema_name)),
                (memory_id, event_id),
            ).fetchone()
            assert row == (0, 0)
    finally:
        if delete_connection is not None:
            delete_connection.close()
        if store is not None:
            store.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
