"""Read legacy control receipts without migrating them or relaxing current provenance."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.infrastructure.memory.postgres_admin import normalize_psycopg_dsn
from evaluation.formation import PostgresFormationInspector
from evaluation.models import (
    HISTORICAL_CONTROL_SHA,
    BenchmarkVariant,
    GitSource,
    RunProvenance,
)

pytestmark = pytest.mark.postgres_integration


@pytest.mark.parametrize("has_memory", [False, True])
async def test_historical_receipt_reader_preserves_original_schema_and_owner(has_memory):
    raw_url = os.environ.get("POSTGRES_TEST_URL")
    if not raw_url:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    dsn = normalize_psycopg_dsn(raw_url)
    schema = f"benchmark_receipt_{uuid4().hex}"
    event_id, conversation_id, memory_id = uuid4(), uuid4(), uuid4()
    user_id = "eval:legacy-inspector:owned-user"
    memories = (
        [{"id": str(memory_id), "event": "ADD", "memory": "Standing threshold 98%"}]
        if has_memory
        else []
    )
    provenance = RunProvenance(
        variant=BenchmarkVariant.HISTORICAL_CONTROL,
        runtime=GitSource(sha=HISTORICAL_CONTROL_SHA, dirty=False),
        harness=GitSource(sha="a" * 40, dirty=False),
        prompt_sha256={"memory_extraction": "b" * 64, "rewrite_system": "c" * 64},
        package_versions={"kira-context-memory": "0.4.1", "viettel-mem0": "2.0.20+viettel.3"},
    )
    try:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(
                sql.SQL("CREATE TABLE {} (id UUID PRIMARY KEY, payload JSONB)").format(
                    sql.Identifier(schema, "memories")
                )
            )
            # These are the receipt columns actually recorded by the frozen control runtime.
            connection.execute(
                sql.SQL(
                    "CREATE TABLE {} (event_id UUID PRIMARY KEY, user_id TEXT NOT NULL, "
                    "result JSONB NOT NULL, memory_count INTEGER NOT NULL, "
                    "committed_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                ).format(sql.Identifier(schema, "memories_formation_receipts"))
            )
            connection.execute(
                sql.SQL("INSERT INTO {} VALUES (%s, %s, %s, %s, now())").format(
                    sql.Identifier(schema, "memories_formation_receipts")
                ),
                (event_id, user_id, Jsonb(memories), len(memories)),
            )
            if has_memory:
                connection.execute(
                    sql.SQL("INSERT INTO {} VALUES (%s, %s)").format(
                        sql.Identifier(schema, "memories")
                    ),
                    (
                        memory_id,
                        Jsonb(
                            {
                                "formation_event_id": str(event_id),
                                "user_id": user_id,
                                "conversation_id": str(conversation_id),
                                "turn_id": "source-user-message",
                                "boundary_message_id": 91,
                                "data": memories[0]["memory"],
                            }
                        ),
                    ),
                )
        inspector = PostgresFormationInspector(
            dsn, schema_name=schema, collection_name="memories", runtime_provenance=provenance
        )
        snapshot = await inspector.inspect(event_id=event_id, user_id=user_id)
        assert snapshot.receipt.receipt_contract == "historical_control_75deb1d8"
        assert snapshot.receipt.conversation_id is None
        assert snapshot.receipt.memory_count == len(memories)
        assert len(snapshot.memories) == len(memories)
        if has_memory:
            assert snapshot.memories[0].conversation_id == conversation_id
        with pytest.raises(ValueError, match="owner|identity"):
            await inspector.inspect(event_id=event_id, user_id="unrelated-user")
        current = PostgresFormationInspector(dsn, schema_name=schema, collection_name="memories")
        with pytest.raises(psycopg.errors.UndefinedColumn):
            await current.inspect(event_id=event_id, user_id=user_id)
        with psycopg.connect(dsn) as connection:
            columns = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'memories_formation_receipts' "
                "ORDER BY ordinal_position",
                (schema,),
            ).fetchall()
            assert [row[0] for row in columns] == [
                "event_id",
                "user_id",
                "result",
                "memory_count",
                "committed_at",
            ]
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
