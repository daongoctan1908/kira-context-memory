"""Filter-before-top-k and entity-boost scope isolation on real pgvector."""

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


def _memory_payload(
    memory_id: str,
    *,
    user_id: str,
    conversation_id: UUID,
    data: str,
    memory_scope: str,
) -> dict:
    return {
        "id": memory_id,
        "user_id": user_id,
        "conversation_id": str(conversation_id),
        "run_id": str(conversation_id),
        "memory_scope": memory_scope,
        "formation_event_id": str(uuid4()),
        "data": data,
        "text_lemmatized": data,
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


ENTITY = ("METRIC", "Tỷ lệ giữ chân")


async def test_filters_apply_before_top_k_in_both_scoped_branches() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_scope_topk_{uuid4().hex}"
    user_id = f"scope-user-{uuid4().hex}"
    other_user = f"scope-other-{uuid4().hex}"
    conversation = uuid4()
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

        # One in-scope target plus 8 out-of-scope distractors per filter axis.
        # Distractors carry an orthogonal vector with a distance strictly worse
        # than the target's self-similarity: if top-k were applied before
        # filters, the 2 limit window would fill with distractors regardless.
        payloads = [
            _memory_payload(
                str(uuid4()),
                user_id=user_id,
                conversation_id=conversation,
                data="in-scope target",
                memory_scope="CONVERSATION",
            )
        ]
        for index in range(8):
            payloads.append(
                _memory_payload(
                    str(uuid4()),
                    user_id=user_id,
                    conversation_id=uuid4(),
                    data=f"wrong conversation {index}",
                    memory_scope="CONVERSATION",
                )
            )
        for index in range(8):
            payloads.append(
                _memory_payload(
                    str(uuid4()),
                    user_id=user_id,
                    conversation_id=conversation,
                    data=f"global scope {index}",
                    memory_scope="GLOBAL",
                )
            )
        for index in range(8):
            payloads.append(
                _memory_payload(
                    str(uuid4()),
                    user_id=other_user,
                    conversation_id=conversation,
                    data=f"other user {index}",
                    memory_scope="CONVERSATION",
                )
            )

        store = _vector_store(dsn, schema_name, fenced=True)
        store.insert(
            [[1.0, 0.0, 0.0]]
            + [[0.0, 1.0, 0.0]] * (len(payloads) - 1),
            payloads=payloads,
            ids=[payload["id"] for payload in payloads],
        )

        # top_k=2 keeps headroom below the internal over-fetch; a post-filter
        # implementation would return 2 distractors here, never the target.
        conversation_results = store.search(
            "in-scope target",
            [1.0, 0.0, 0.0],
            top_k=2,
            filters={
                "user_id": user_id,
                "run_id": str(conversation),
                "memory_scope": "CONVERSATION",
            },
        )
        assert [row.payload["data"] for row in conversation_results] == ["in-scope target"]

        global_payloads = [
            _memory_payload(
                str(uuid4()),
                user_id=user_id,
                conversation_id=conversation,
                data="global target",
                memory_scope="GLOBAL",
            )
        ]
        # The 8 GLOBAL distractors are already persisted from the first insert.
        store.insert(
            [[1.0, 0.0, 0.0]] * len(global_payloads),
            payloads=global_payloads,
            ids=[payload["id"] for payload in global_payloads],
        )
        global_results = store.search(
            "global target",
            [1.0, 0.0, 0.0],
            top_k=2,
            filters={"user_id": user_id, "memory_scope": "GLOBAL"},
        )
        assert global_results[0].payload["data"] == "global target"
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE user_id = ANY(%s)",
                ([user_id, other_user],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_adapter_search_scoped_returns_only_in_scope_rows() -> None:
    dsn = _test_dsn()
    schema_name = f"memory_scope_adapter_{uuid4().hex}"
    user_id = f"adapter-user-{uuid4().hex}"
    other_user = f"adapter-other-{uuid4().hex}"
    conversation = uuid4()
    other_conversation = uuid4()
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
            _insert_conversation(connection, other_conversation, user_id)

        payloads = [
            _memory_payload(
                str(uuid4()),
                user_id=user_id,
                conversation_id=conversation,
                data="conversation fact",
                memory_scope="CONVERSATION",
            ),
            _memory_payload(
                str(uuid4()),
                user_id=user_id,
                conversation_id=other_conversation,
                data="other conversation fact",
                memory_scope="CONVERSATION",
            ),
            _memory_payload(
                str(uuid4()),
                user_id=other_user,
                conversation_id=conversation,
                data="cross-user fact",
                memory_scope="CONVERSATION",
            ),
            _memory_payload(
                str(uuid4()),
                user_id=user_id,
                conversation_id=conversation,
                data="global fact",
                memory_scope="GLOBAL",
            ),
        ]
        store = _vector_store(dsn, schema_name, fenced=True)
        store.insert(
            [[1.0, 0.0, 0.0]] * len(payloads),
            payloads=payloads,
            ids=[payload["id"] for payload in payloads],
        )

        adapter = _patched_client(_settings(dsn, schema_name))

        local = await adapter.search_scoped(
            user_id,
            "fact",
            conversation_id=conversation,
            scope="conversation",
            top_k=10,
            threshold=0.0,
        )
        assert [memory.content for memory in local] == ["conversation fact"]
        assert all(memory.metadata["memory_scope"] == "CONVERSATION" for memory in local)

        global_memories = await adapter.search_scoped(
            user_id,
            "fact",
            conversation_id=conversation,
            scope="global",
            top_k=10,
            threshold=0.0,
        )
        assert [memory.content for memory in global_memories] == ["global fact"]

        assert await adapter.search_scoped(
            other_user,
            "fact",
            conversation_id=other_conversation,
            scope="global",
            top_k=10,
            threshold=0.0,
        ) == ()
    finally:
        if adapter is not None:
            adapter.close()
        elif store is not None:
            store.close()
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "DELETE FROM public.conversations WHERE user_id = ANY(%s)",
                ([user_id, other_user],),
            )
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


async def test_entity_boost_ranking_in_global_branch_is_invariant_to_local_only_entity() -> None:
    """Fix C regression: a local-only entity must not rescale GLOBAL scores."""
    dsn = _test_dsn()
    schema_name = f"memory_scope_entity_{uuid4().hex}"
    user_id = f"entity-user-{uuid4().hex}"
    local_conversation = uuid4()
    remote_conversation = uuid4()
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
            _insert_conversation(connection, local_conversation, user_id)
            _insert_conversation(connection, remote_conversation, user_id)

        # C1 is conversation-local in a different conversation than the search;
        # G1/G2 are user-global. The entity row links only to C1.
        c1_id = str(uuid4())
        g1_id = str(uuid4())
        g2_id = str(uuid4())
        payloads = [
            _memory_payload(
                c1_id,
                user_id=user_id,
                conversation_id=local_conversation,
                data="local-only fact",
                memory_scope="CONVERSATION",
            ),
            _memory_payload(
                g1_id,
                user_id=user_id,
                conversation_id=remote_conversation,
                data="global fact one",
                memory_scope="GLOBAL",
            ),
            _memory_payload(
                g2_id,
                user_id=user_id,
                conversation_id=remote_conversation,
                data="global fact two",
                memory_scope="GLOBAL",
            ),
        ]
        store = _vector_store(dsn, schema_name, fenced=True)
        store.insert(
            [[1.0, 0.0, 0.0]] * len(payloads),
            payloads=payloads,
            ids=[payload["id"] for payload in payloads],
        )

        adapter = _patched_client(_settings(dsn, schema_name))
        entity_store = adapter._client.entity_store
        # Entity insert passes through _filter_active_entity_links, so the link
        # to C1 survives only while C1's owner conversation stays active.
        entity_store.insert(
            vectors=[[1.0, 0.0, 0.0]],
            ids=[str(uuid4())],
            payloads=[
                {
                    "data": ENTITY[1],
                    "entity_type": ENTITY[0],
                    "linked_memory_ids": [c1_id],
                    "user_id": user_id,
                }
            ],
        )

        with patch("mem0.memory.main.extract_entities", return_value=[ENTITY]):
            global_with_entity = await adapter.search_scoped(
                user_id,
                QUERY,
                conversation_id=remote_conversation,
                scope="global",
                top_k=10,
                threshold=0.0,
            )

        for batch in entity_store.list(filters={"user_id": user_id}, top_k=100):
            for row in batch:
                entity_store.delete(vector_id=row.id)

        with patch("mem0.memory.main.extract_entities", return_value=[ENTITY]):
            global_without_entity = await adapter.search_scoped(
                user_id,
                QUERY,
                conversation_id=remote_conversation,
                scope="global",
                top_k=10,
                threshold=0.0,
            )

        assert [memory.memory_id for memory in global_with_entity] == [g1_id, g2_id]
        assert [memory.memory_id for memory in global_without_entity] == [g1_id, g2_id]
        for first, second in zip(global_with_entity, global_without_entity, strict=True):
            assert abs(first.score - second.score) < 1e-6

        # The conversation-local memory must never leak into the global branch.
        assert c1_id not in {memory.memory_id for memory in global_with_entity}
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
