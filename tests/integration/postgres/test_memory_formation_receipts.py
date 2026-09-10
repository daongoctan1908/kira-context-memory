"""Real pgvector transaction gates for event-scoped memory formation receipts."""

import asyncio
import os
from functools import partial
from uuid import uuid4

import psycopg
import pytest
from mem0.vector_stores.pgvector import PGVector
from psycopg import sql

from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    normalize_psycopg_dsn,
)

pytestmark = pytest.mark.postgres_integration


def _test_dsn() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return normalize_psycopg_dsn(value)


def _vector_store(dsn: str, schema_name: str) -> PGVector:
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
    )


async def test_receipt_conflict_ignores_paraphrase_and_never_inserts_second_memory() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_receipt_{uuid4().hex}"
    event_id = str(uuid4())
    first_id = str(uuid4())
    second_id = str(uuid4())
    store = None
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        store = _vector_store(dsn, schema_name)
        first = [{"id": first_id, "memory": "User prefers threshold 10%", "event": "ADD"}]
        paraphrase = [
            {
                "id": second_id,
                "memory": "User prefers a threshold of ten percent",
                "event": "ADD",
            }
        ]

        created, committed = store.insert_with_formation_receipt(
            [[1.0, 0.0, 0.0]],
            [
                {
                    "user_id": "user-1",
                    "formation_event_id": event_id,
                    "data": first[0]["memory"],
                }
            ],
            [first_id],
            event_id=event_id,
            user_id="user-1",
            result=first,
        )
        duplicate_created, duplicate_result = store.insert_with_formation_receipt(
            [[0.0, 1.0, 0.0]],
            [
                {
                    "user_id": "user-1",
                    "formation_event_id": event_id,
                    "data": paraphrase[0]["memory"],
                }
            ],
            [second_id],
            event_id=event_id,
            user_id="user-1",
            result=paraphrase,
        )

        assert created is True
        assert committed == first
        assert duplicate_created is False
        assert duplicate_result == first
        assert store.get_formation_result(event_id, "user-1") == first
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT count(*), count(DISTINCT payload->>'formation_event_id') "
                    "FROM {}.memories WHERE payload->>'formation_event_id' = %s"
                ).format(sql.Identifier(schema_name)),
                (event_id,),
            )
            assert cursor.fetchone() == (1, 1)
            cursor.execute(
                sql.SQL(
                    "SELECT memory_count FROM {}.memories_formation_receipts WHERE event_id = %s"
                ).format(sql.Identifier(schema_name)),
                (event_id,),
            )
            assert cursor.fetchone() == (1,)
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_failed_batch_rolls_back_memories_and_receipt_then_retry_can_commit() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_rollback_{uuid4().hex}"
    event_id = str(uuid4())
    duplicated_id = str(uuid4())
    store = None
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        store = _vector_store(dsn, schema_name)
        results = [
            {"id": duplicated_id, "memory": "first fact", "event": "ADD"},
            {"id": duplicated_id, "memory": "second fact", "event": "ADD"},
        ]
        payloads = [
            {
                "user_id": "user-1",
                "formation_event_id": event_id,
                "data": item["memory"],
            }
            for item in results
        ]

        with pytest.raises(psycopg.errors.UniqueViolation):
            store.insert_with_formation_receipt(
                [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                payloads,
                [duplicated_id, duplicated_id],
                event_id=event_id,
                user_id="user-1",
                result=results,
            )

        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT "
                    "(SELECT count(*) FROM {}.memories WHERE payload->>'formation_event_id' = %s), "
                    "(SELECT count(*) FROM {}.memories_formation_receipts WHERE event_id = %s)"
                ).format(sql.Identifier(schema_name), sql.Identifier(schema_name)),
                (event_id, event_id),
            )
            assert cursor.fetchone() == (0, 0)

        retry_id = str(uuid4())
        retry_result = [{"id": retry_id, "memory": "retry fact", "event": "ADD"}]
        created, committed = store.insert_with_formation_receipt(
            [[0.0, 0.0, 1.0]],
            [
                {
                    "user_id": "user-1",
                    "formation_event_id": event_id,
                    "data": "retry fact",
                }
            ],
            [retry_id],
            event_id=event_id,
            user_id="user-1",
            result=retry_result,
        )
        assert created is True
        assert committed == retry_result
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_concurrent_same_event_elects_one_complete_formation() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_concurrent_receipt_{uuid4().hex}"
    event_id = str(uuid4())
    store = None
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            schema_name,
            "memories",
            "test-embedding-model",
            3,
        )
        store = _vector_store(dsn, schema_name)

        calls = []
        for wording, vector in (
            ("User prefers threshold 10%", [1.0, 0.0, 0.0]),
            ("User prefers a threshold of ten percent", [0.0, 1.0, 0.0]),
        ):
            memory_id = str(uuid4())
            result = [{"id": memory_id, "memory": wording, "event": "ADD"}]
            calls.append(
                partial(
                    store.insert_with_formation_receipt,
                    [vector],
                    [
                        {
                            "user_id": "user-1",
                            "formation_event_id": event_id,
                            "data": wording,
                        }
                    ],
                    [memory_id],
                    event_id=event_id,
                    user_id="user-1",
                    result=result,
                )
            )

        outcomes = await asyncio.gather(*(asyncio.to_thread(call) for call in calls))

        assert sorted(created for created, _ in outcomes) == [False, True]
        assert outcomes[0][1] == outcomes[1][1]
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT "
                    "(SELECT count(*) FROM {}.memories WHERE payload->>'formation_event_id' = %s), "
                    "(SELECT count(*) FROM {}.memories_formation_receipts WHERE event_id = %s)"
                ).format(sql.Identifier(schema_name), sql.Identifier(schema_name)),
                (event_id, event_id),
            )
            assert cursor.fetchone() == (1, 1)
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
