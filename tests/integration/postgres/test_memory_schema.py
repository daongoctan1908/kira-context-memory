import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Json

from app.domain.errors.memory import LongTermMemoryConfigurationError
from app.infrastructure.memory.postgres_admin import (
    LEGACY_MEM0_VERSION,
    LEGACY_MEMORY_SCHEMA_VERSION,
    MEMORY_SCHEMA_VERSION,
    PREVIOUS_MEM0_SCHEMA_CONTRACT_VERSION,
    PREVIOUS_MEMORY_SCHEMA_VERSION,
    _initialize_memory_schema_sync,
    _validate_memory_schema_sync,
    normalize_psycopg_dsn,
)

pytestmark = pytest.mark.postgres_integration


def _test_dsn() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return normalize_psycopg_dsn(value)


def _downgrade_receipts_to_v2(cursor, schema_name: str) -> None:
    cursor.execute(
        sql.SQL("DROP INDEX {}.{}").format(
            sql.Identifier(schema_name),
            sql.Identifier("memories_formation_receipt_owner_idx"),
        )
    )
    cursor.execute(
        sql.SQL("ALTER TABLE {}.memories_formation_receipts DROP COLUMN conversation_id").format(
            sql.Identifier(schema_name)
        )
    )
    cursor.execute(
        sql.SQL(
            "UPDATE {}.kira_memory_schema SET schema_version = %s, mem0_version = %s "
            "WHERE singleton"
        ).format(sql.Identifier(schema_name)),
        (PREVIOUS_MEMORY_SCHEMA_VERSION, PREVIOUS_MEM0_SCHEMA_CONTRACT_VERSION),
    )


async def test_memory_schema_init_is_idempotent_and_validates_dimension() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_test_{uuid4().hex}"
    collection_name = "memories"
    try:
        state = await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            collection_name,
            "test-embedding-model",
            3,
        )
        repeated = await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            collection_name,
            "test-embedding-model",
            3,
        )

        assert state == repeated
        validated = await asyncio.to_thread(
            _validate_memory_schema_sync,
            dsn,
            schema_name,
            collection_name,
            "test-embedding-model",
            3,
        )
        assert validated == state
        assert state.schema_version == MEMORY_SCHEMA_VERSION
        assert state.embedding_dims == 3
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name",
                (schema_name,),
            )
            assert [row[0] for row in cursor.fetchall()] == [
                "kira_memory_schema",
                "memories",
                "memories_entities",
                "memories_formation_receipts",
            ]
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
                (schema_name,),
            )
            indexes = {row[0] for row in cursor.fetchall()}
            assert "memories_hnsw_idx" in indexes
            assert "memories_formation_event_id_idx" in indexes
            assert "memories_user_id_idx" in indexes
            assert "memories_formation_receipt_owner_idx" in indexes

            cursor.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema_name, "memories_formation_receipts"),
            )
            assert {row[0]: (row[1], row[2]) for row in cursor.fetchall()} == {
                "event_id": ("uuid", "NO"),
                "user_id": ("text", "NO"),
                "conversation_id": ("uuid", "NO"),
                "result": ("jsonb", "NO"),
                "memory_count": ("integer", "NO"),
                "committed_at": ("timestamp with time zone", "NO"),
            }

        with pytest.raises(LongTermMemoryConfigurationError):
            await asyncio.to_thread(
                _initialize_memory_schema_sync,
                dsn,
                schema_name,
                collection_name,
                "different-model",
                3,
            )
        with pytest.raises(LongTermMemoryConfigurationError):
            await asyncio.to_thread(
                _validate_memory_schema_sync,
                dsn,
                schema_name,
                collection_name,
                "different-model",
                3,
            )
    finally:
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_memory_schema_upgrades_v1_receipt_contract_without_losing_vectors() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_upgrade_{uuid4().hex}"
    collection_name = "memories"
    memory_id = uuid4()
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            collection_name,
            "test-embedding-model",
            3,
        )
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("INSERT INTO {}.{} (id, vector, payload) VALUES (%s, %s, %s)").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(collection_name),
                ),
                (memory_id, [1.0, 0.0, 0.0], Json({"user_id": "upgrade-user"})),
            )
            cursor.execute(
                sql.SQL("DROP TABLE {}.{}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier("memories_formation_receipts"),
                )
            )
            cursor.execute(
                sql.SQL("DROP INDEX {}.{}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier("memories_formation_event_id_idx"),
                )
            )
            cursor.execute(
                sql.SQL(
                    "UPDATE {}.kira_memory_schema "
                    "SET schema_version = %s, mem0_version = %s WHERE singleton"
                ).format(sql.Identifier(schema_name)),
                (LEGACY_MEMORY_SCHEMA_VERSION, LEGACY_MEM0_VERSION),
            )

        state = await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            collection_name,
            "test-embedding-model",
            3,
        )

        assert state.schema_version == MEMORY_SCHEMA_VERSION
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT payload->>'user_id' FROM {}.{} WHERE id = %s").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(collection_name),
                ),
                (memory_id,),
            )
            assert cursor.fetchone() == ("upgrade-user",)
            cursor.execute(
                "SELECT to_regclass(%s), to_regclass(%s)",
                (
                    f"{schema_name}.memories_formation_receipts",
                    f"{schema_name}.memories_formation_event_id_idx",
                ),
            )
            assert all(value is not None for value in cursor.fetchone())
    finally:
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_memory_schema_v2_backfills_receipt_owner_from_vector_or_job() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_owner_upgrade_{uuid4().hex}"
    user_id = f"owner-upgrade-{uuid4().hex}"
    vector_event_id = uuid4()
    vector_conversation_id = uuid4()
    job_event_id = uuid4()
    job_conversation_id = uuid4()
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            _downgrade_receipts_to_v2(cursor, schema_name)
            cursor.execute(
                sql.SQL("INSERT INTO {}.memories (id, vector, payload) VALUES (%s, %s, %s)").format(
                    sql.Identifier(schema_name)
                ),
                (
                    uuid4(),
                    [1.0, 0.0, 0.0],
                    Json(
                        {
                            "user_id": user_id,
                            "formation_event_id": str(vector_event_id),
                            "conversation_id": str(vector_conversation_id),
                            "data": "owned fact",
                        }
                    ),
                ),
            )
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.memories_formation_receipts "
                    "(event_id, user_id, result, memory_count) VALUES (%s, %s, %s, 1)"
                ).format(sql.Identifier(schema_name)),
                (
                    vector_event_id,
                    user_id,
                    Json([{"id": str(uuid4()), "memory": "owned fact", "event": "ADD"}]),
                ),
            )
            cursor.execute(
                "INSERT INTO public.conversations "
                "(conversation_id, user_id, session_id) VALUES (%s, %s, %s)",
                (job_conversation_id, user_id, f"owner-upgrade-{uuid4().hex}"),
            )
            cursor.execute(
                "INSERT INTO public.conversation_messages "
                "(conversation_id, turn_id, turn_sequence, message_index, role, content, "
                "message_timestamp, schema_version) "
                "VALUES (%s, %s, 1, 1, 'assistant', 'answer', now(), 1) "
                "RETURNING message_id",
                (job_conversation_id, f"owner-upgrade-turn-{uuid4().hex}"),
            )
            boundary_message_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO public.memory_jobs (event_id, boundary_message_id) VALUES (%s, %s)",
                (job_event_id, boundary_message_id),
            )
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.memories_formation_receipts "
                    "(event_id, user_id, result, memory_count) VALUES (%s, %s, '[]', 0)"
                ).format(sql.Identifier(schema_name)),
                (job_event_id, user_id),
            )

        state = await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )

        assert state.schema_version == MEMORY_SCHEMA_VERSION
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT event_id, conversation_id FROM {}.memories_formation_receipts "
                    "ORDER BY event_id"
                ).format(sql.Identifier(schema_name))
            )
            owners = dict(cursor.fetchall())
            assert owners == {
                vector_event_id: vector_conversation_id,
                job_event_id: job_conversation_id,
            }
    finally:
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.conversations WHERE conversation_id = %s",
                (job_conversation_id,),
            )
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_memory_schema_v2_refuses_unknown_receipt_owner_without_mutation() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_owner_unknown_{uuid4().hex}"
    event_id = uuid4()
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            _downgrade_receipts_to_v2(cursor, schema_name)
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.memories_formation_receipts "
                    "(event_id, user_id, result, memory_count) VALUES (%s, %s, '[]', 0)"
                ).format(sql.Identifier(schema_name)),
                (event_id, "unknown-owner"),
            )

        with pytest.raises(LongTermMemoryConfigurationError, match=str(event_id)):
            await asyncio.to_thread(
                _initialize_memory_schema_sync,
                dsn,
                schema_name,
                "memories",
                "test-embedding-model",
                3,
            )

        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT schema_version, mem0_version FROM {}.kira_memory_schema WHERE singleton"
                ).format(sql.Identifier(schema_name))
            )
            assert cursor.fetchone() == (
                PREVIOUS_MEMORY_SCHEMA_VERSION,
                PREVIOUS_MEM0_SCHEMA_CONTRACT_VERSION,
            )
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'memories_formation_receipts' "
                "AND column_name = 'conversation_id'",
                (schema_name,),
            )
            assert cursor.fetchone() is None
    finally:
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
