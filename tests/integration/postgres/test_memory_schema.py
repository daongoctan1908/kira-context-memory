import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.domain.errors.memory import LongTermMemoryConfigurationError
from app.infrastructure.memory.postgres_admin import (
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
            ]
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
                (schema_name,),
            )
            indexes = {row[0] for row in cursor.fetchall()}
            assert "memories_hnsw_idx" in indexes
            assert "memories_user_id_idx" in indexes

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
