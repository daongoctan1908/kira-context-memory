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

            cursor.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema_name, "memories_formation_receipts"),
            )
            assert {row[0]: (row[1], row[2]) for row in cursor.fetchall()} == {
                "event_id": ("uuid", "NO"),
                "user_id": ("text", "NO"),
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
