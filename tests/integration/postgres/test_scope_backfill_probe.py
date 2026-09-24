"""Disposable in-place update probe for the scope backfill (Phase 3 gate).

Proves that a plain in-place ``UPDATE ... payload = jsonb_set(...)`` on live
pgvector rows can promote legacy payloads (no ``run_id``, no ``memory_scope``)
to the scoped taxonomy without re-materialization: identity columns, vectors,
formation provenance, receipts, and entity links all survive, the new scoped
filters match and legacy-only filters still work, retrieval + entity boost +
deletion keep working, and a mid-transaction failure rolls back cleanly.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest
from mem0.vector_stores.pgvector import PGVector
from psycopg import sql

from app.config.settings import Settings
from app.infrastructure.memory.mem0_adapter import Mem0Adapter, create_mem0_client
from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    normalize_psycopg_dsn,
)

pytestmark = pytest.mark.postgres_integration

QUERY = "Ngưỡng cảnh báo của Tỷ lệ giữ chân là gì?"
ENTITY = ("METRIC", "Tỷ lệ giữ chân")


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return value


def _test_dsn() -> str:
    return normalize_psycopg_dsn(_test_url())


def _settings(database: str, schema_name: str) -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="http://kira.test",
        kira_username="service-account",
        kira_basic_auth="credential",
        ltm_enabled=True,
        memory_database_url=database,
        memory_schema=schema_name,
        memory_collection_name="memories",
        memory_postgres_min_connections=1,
        memory_postgres_max_connections=3,
        memory_embedding_base_url="http://deterministic-embedding.test",
        memory_embedding_model="deterministic-embedding-v1",
        memory_embedding_dims=3,
        memory_llm_base_url="http://deterministic-memory-llm.test",
        memory_llm_model="deterministic-memory-llm-v1",
        memory_search_threshold=0,
    )


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
    status: str = "active",
) -> None:
    connection.execute(
        "INSERT INTO public.conversations "
        "(conversation_id, user_id, session_id, status) VALUES (%s, %s, %s, %s)",
        (conversation_id, user_id, uuid4().hex, status),
    )


def _legacy_payload(
    memory_id: str,
    *,
    user_id: str,
    conversation_id: UUID,
    event_id: str,
    data: str,
) -> dict:
    """Pre-scope payload: no run_id, no memory_scope — the legacy shape."""
    return {
        "id": memory_id,
        "user_id": user_id,
        "conversation_id": str(conversation_id),
        "formation_event_id": event_id,
        "data": data,
        "text_lemmatized": data,
        "hash": f"hash-{memory_id}",
        "attributed_to": "user",
    }


class FixedEmbedding:
    def __init__(self) -> None:
        self.config = SimpleNamespace(embedding_dims=3)

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        return [1.0, 0.0, 0.0]

    def embed_batch(self, texts: list[str], memory_action: str = "add") -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class SilentMemoryLlm:
    def generate_response(self, messages, response_format=None, **kwargs):
        return json.dumps({"memory": []}, ensure_ascii=False)


def _patched_client(settings: Settings) -> Mem0Adapter:
    with (
        patch("mem0.memory.main.EmbedderFactory.create", return_value=FixedEmbedding()),
        patch("mem0.memory.main.LlmFactory.create", return_value=SilentMemoryLlm()),
    ):
        return Mem0Adapter(
            create_mem0_client(settings),
            search_timeout_seconds=settings.memory_search_timeout_seconds,
            operation_timeout_seconds=settings.memory_operation_timeout_seconds,
        )


def _apply_scope_backfill(
    dsn: str,
    schema_name: str,
    *,
    memory_id: str,
    run_id: str,
    memory_scope: str,
    fail_before_commit: bool = False,
) -> None:
    """Run the probe's in-place backfill exactly as Phase 4 will: one
    transaction, jsonb_set per field, row-locked, committed only at the end."""
    with psycopg.connect(dsn) as connection:
        with connection.transaction():
            connection.execute(
                sql.SQL("SELECT id, payload FROM {} WHERE id = %s FOR UPDATE").format(
                    sql.Identifier(schema_name, "memories")
                ),
                (memory_id,),
            )
            connection.execute(
                sql.SQL(
                    "UPDATE {} SET payload = jsonb_set(jsonb_set(payload, "
                    "'{{run_id}}', to_jsonb(%s::text), true), "
                    "'{{memory_scope}}', to_jsonb(%s::text), true) WHERE id = %s"
                ).format(sql.Identifier(schema_name, "memories")),
                (run_id, memory_scope, memory_id),
            )
            if fail_before_commit:
                raise RuntimeError("probe failure injected before commit")


def _fetch_payload(dsn: str, schema_name: str, memory_id: str) -> dict:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            sql.SQL("SELECT payload FROM {} WHERE id = %s").format(
                sql.Identifier(schema_name, "memories")
            ),
            (memory_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def _fetch_receipt(dsn: str, schema_name: str, event_id: str) -> tuple | None:
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            sql.SQL(
                "SELECT user_id, conversation_id::text, result, memory_count "
                "FROM {} WHERE event_id = %s"
            ).format(sql.Identifier(schema_name, "memories_formation_receipts")),
            (event_id,),
        ).fetchone()


def _entity_row(dsn: str, schema_name: str, user_id: str) -> dict | None:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            sql.SQL(
                "SELECT payload FROM {}.memories_entities WHERE payload->>'user_id' = %s"
            ).format(sql.Identifier(schema_name)),
            (user_id,),
        ).fetchone()
    return row[0] if row else None


async def test_in_place_scope_backfill_preserves_identity_and_transitions_filters() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_probe_backfill_{uuid4().hex}"
    user_id = f"probe-user-{uuid4().hex}"
    conversation = uuid4()
    event_id = str(uuid4())
    memory_id = str(uuid4())
    store = adapter = None
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
            _insert_conversation(connection, conversation, user_id)

        # Seed the legacy row through the real formation path so the receipt
        # exists exactly as it would in production.
        store = _vector_store(dsn, schema_name, fenced=True)
        store.insert_with_formation_receipt(
            [[1.0, 0.0, 0.0]],
            [
                _legacy_payload(
                    memory_id,
                    user_id=user_id,
                    conversation_id=conversation,
                    event_id=event_id,
                    data="legacy fact",
                )
            ],
            [memory_id],
            event_id=event_id,
            user_id=user_id,
            conversation_id=str(conversation),
            result=[{"id": memory_id, "memory": "legacy fact", "event": "ADD"}],
        )
        # Entity row links the (still unscoped) memory; links survive only
        # while the owner conversation is active.
        adapter = _patched_client(_settings(dsn, schema_name))
        adapter._client.entity_store.insert(
            vectors=[[1.0, 0.0, 0.0]],
            ids=[str(uuid4())],
            payloads=[
                {
                    "data": ENTITY[1],
                    "entity_type": ENTITY[0],
                    "linked_memory_ids": [memory_id],
                    "user_id": user_id,
                }
            ],
        )

        legacy_payload = _fetch_payload(dsn, schema_name, memory_id)
        assert "run_id" not in legacy_payload
        assert "memory_scope" not in legacy_payload
        legacy_receipt = _fetch_receipt(dsn, schema_name, event_id)
        assert legacy_receipt is not None

        _apply_scope_backfill(
            dsn,
            schema_name,
            memory_id=memory_id,
            run_id=str(conversation),
            memory_scope="CONVERSATION",
        )

        backfilled = _fetch_payload(dsn, schema_name, memory_id)
        # Identity fields untouched by jsonb_set.
        assert backfilled["id"] == legacy_payload["id"]
        assert backfilled["user_id"] == legacy_payload["user_id"]
        assert backfilled["conversation_id"] == legacy_payload["conversation_id"]
        assert backfilled["formation_event_id"] == legacy_payload["formation_event_id"]
        assert backfilled["data"] == legacy_payload["data"]
        assert backfilled["text_lemmatized"] == legacy_payload["text_lemmatized"]
        assert backfilled["hash"] == legacy_payload["hash"]
        assert backfilled["attributed_to"] == legacy_payload["attributed_to"]
        # Only the two scope fields were added.
        assert backfilled["run_id"] == str(conversation)
        assert backfilled["memory_scope"] == "CONVERSATION"
        assert set(backfilled) == set(legacy_payload) | {"run_id", "memory_scope"}

        # Receipt untouched; identity contract still holds.
        assert _fetch_receipt(dsn, schema_name, event_id) == legacy_receipt
        assert _fetch_receipt(dsn, schema_name, event_id)[:2] == (user_id, str(conversation))

        # Entity links intact and still resolvable through the fenced filter.
        assert _entity_row(dsn, schema_name, user_id)["linked_memory_ids"] == [memory_id]

        # The vector and text are byte-identical to pre-update state.
        vector = adapter._client.vector_store.get(vector_id=memory_id)
        assert vector is not None

        # Filter transition: the row left the legacy shape (no longer
        # selectable by a backfill that targets run_id-less rows), the
        # user_id-only legacy filters still reach it, and the new scoped
        # filters match.
        with psycopg.connect(dsn) as connection:
            table = sql.Identifier(schema_name, "memories")
            legacy_shape = connection.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} WHERE payload->>'user_id' = %s "
                    "AND NOT (payload ? 'run_id')"
                ).format(table),
                (user_id,),
            ).fetchone()[0]
            owner_only = connection.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE payload->>'user_id' = %s").format(table),
                (user_id,),
            ).fetchone()[0]
            scoped_match = connection.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} WHERE payload->>'user_id' = %s "
                    "AND payload->>'run_id' = %s AND payload->>'memory_scope' = %s"
                ).format(table),
                (user_id, str(conversation), "CONVERSATION"),
            ).fetchone()[0]
        assert legacy_shape == 0
        assert owner_only == 1
        assert scoped_match == 1

        results = store.search(
            "legacy fact",
            [1.0, 0.0, 0.0],
            top_k=5,
            filters={
                "user_id": user_id,
                "run_id": str(conversation),
                "memory_scope": "CONVERSATION",
            },
        )
        assert [row.payload["data"] for row in results] == ["legacy fact"]
    finally:
        if adapter is not None:
            adapter.close()
        elif store is not None:
            store.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE user_id = ANY(%s)",
                ([user_id],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_in_place_backfill_survives_retrieval_entity_boost_and_deletion() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_probe_fullpath_{uuid4().hex}"
    user_id = f"probe-user-{uuid4().hex}"
    conversation = uuid4()
    event_id = str(uuid4())
    memory_id = str(uuid4())
    store = adapter = None
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
            _insert_conversation(connection, conversation, user_id)

        store = _vector_store(dsn, schema_name, fenced=True)
        # Seed with a vector that is NOT self-similar to the query embedding
        # ([1, 0, 0] from FixedEmbedding) so the semantic score sits strictly
        # below 1.0 and an entity boost is observable as a score increase.
        store.insert_with_formation_receipt(
            [[0.9, 0.1, 0.0]],
            [
                _legacy_payload(
                    memory_id,
                    user_id=user_id,
                    conversation_id=conversation,
                    event_id=event_id,
                    data="legacy fact",
                )
            ],
            [memory_id],
            event_id=event_id,
            user_id=user_id,
            conversation_id=str(conversation),
            result=[{"id": memory_id, "memory": "legacy fact", "event": "ADD"}],
        )
        adapter = _patched_client(_settings(dsn, schema_name))
        # Entity payload mirrors production shape (search_filters spread):
        # user_id + run_id, so post-backfill entity lookups (filtered by
        # user_id AND run_id) still resolve the row.
        adapter._client.entity_store.insert(
            vectors=[[1.0, 0.0, 0.0]],
            ids=[str(uuid4())],
            payloads=[
                {
                    "data": ENTITY[1],
                    "entity_type": ENTITY[0],
                    "linked_memory_ids": [memory_id],
                    "user_id": user_id,
                    "run_id": str(conversation),
                }
            ],
        )

        _apply_scope_backfill(
            dsn,
            schema_name,
            memory_id=memory_id,
            run_id=str(conversation),
            memory_scope="CONVERSATION",
        )

        # 1) Scoped retrieval through the full adapter pipeline finds the
        # backfilled row in the conversation branch.
        with patch("mem0.memory.main.extract_entities", return_value=[]):
            local = await adapter.search_scoped(
                user_id,
                QUERY,
                conversation_id=conversation,
                scope="conversation",
                top_k=10,
                threshold=0.0,
            )
        assert [memory.memory_id for memory in local] == [memory_id]
        assert local[0].metadata["memory_scope"] == "CONVERSATION"
        assert local[0].score < 1.0

        # 2) Entity boost still applies after backfill: the entity search
        # filter set (user_id/run_id) resolves the entity row and boosts the
        # linked memory — score strictly above the unboosted semantic score.
        with patch("mem0.memory.main.extract_entities", return_value=[ENTITY]):
            boosted = await adapter.search_scoped(
                user_id,
                QUERY,
                conversation_id=conversation,
                scope="conversation",
                top_k=10,
                threshold=0.0,
            )
        assert [memory.memory_id for memory in boosted] == [memory_id]
        assert boosted[0].score > local[0].score

        # 3) Deletion still works end-to-end: adapter delete removes the row
        # and prunes the entity link that pointed at it.
        await adapter._client.delete(memory_id)
        with psycopg.connect(dsn) as connection:
            remaining = connection.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE id = %s").format(
                    sql.Identifier(schema_name, "memories")
                ),
                (memory_id,),
            ).fetchone()[0]
        assert remaining == 0
        assert _entity_row(dsn, schema_name, user_id) is None

        # 4) Formation receipt replay identity: the receipt remains readable
        # through the store contract after the backfill.
        assert store.get_formation_result(event_id, user_id, str(conversation)) == [
            {"id": memory_id, "memory": "legacy fact", "event": "ADD"}
        ]
    finally:
        if adapter is not None:
            adapter.close()
        elif store is not None:
            store.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE user_id = ANY(%s)",
                ([user_id],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_backfill_rollback_on_mid_transaction_failure_leaves_payload_unchanged() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_probe_rollback_{uuid4().hex}"
    user_id = f"probe-user-{uuid4().hex}"
    conversation = uuid4()
    event_id = str(uuid4())
    memory_id = str(uuid4())
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
        with psycopg.connect(dsn) as connection:
            _insert_conversation(connection, conversation, user_id)

        store = _vector_store(dsn, schema_name, fenced=True)
        store.insert_with_formation_receipt(
            [[1.0, 0.0, 0.0]],
            [
                _legacy_payload(
                    memory_id,
                    user_id=user_id,
                    conversation_id=conversation,
                    event_id=event_id,
                    data="legacy fact",
                )
            ],
            [memory_id],
            event_id=event_id,
            user_id=user_id,
            conversation_id=str(conversation),
            result=[{"id": memory_id, "memory": "legacy fact", "event": "ADD"}],
        )
        before = _fetch_payload(dsn, schema_name, memory_id)
        before_receipt = _fetch_receipt(dsn, schema_name, event_id)

        with pytest.raises(RuntimeError, match="probe failure"):
            _apply_scope_backfill(
                dsn,
                schema_name,
                memory_id=memory_id,
                run_id=str(conversation),
                memory_scope="CONVERSATION",
                fail_before_commit=True,
            )

        # Nothing leaked: the payload is byte-identical, the receipt intact.
        assert _fetch_payload(dsn, schema_name, memory_id) == before
        assert _fetch_receipt(dsn, schema_name, event_id) == before_receipt

        # The same backfill succeeds immediately after the rollback.
        _apply_scope_backfill(
            dsn,
            schema_name,
            memory_id=memory_id,
            run_id=str(conversation),
            memory_scope="CONVERSATION",
        )
        after = _fetch_payload(dsn, schema_name, memory_id)
        assert after["run_id"] == str(conversation)
        assert after["memory_scope"] == "CONVERSATION"
        assert after["hash"] == before["hash"]
        assert after["formation_event_id"] == before["formation_event_id"]
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE user_id = ANY(%s)",
                ([user_id],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
