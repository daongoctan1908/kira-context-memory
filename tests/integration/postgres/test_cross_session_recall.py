"""Cross-session recall through real PostgreSQL/pgvector and native Mem0 V3."""

import asyncio
import json
import os
import re
import unicodedata
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.services.context_builder import ContextBuilder
from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.config.settings import Settings
from app.domain.errors.memory import LongTermMemoryConnectionError
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.llm.vllm_query_rewriter import VllmQueryRewriterAdapter
from app.infrastructure.memory.mem0_adapter import Mem0Adapter, create_mem0_client
from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    initialize_memory_schema,
    normalize_psycopg_dsn,
)
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import conversations
from app.presentation.api.main import create_app
from scripts.check_live_memory_policy import score_case
from tests.integration.test_gateway_api import FakeKiraClient, kira_event
from tests.support.memory_policy_cases import CASES

pytestmark = pytest.mark.postgres_integration

FACT = (
    "Tỷ lệ giữ chân = (thuê bao cuối kỳ - thuê bao mới) / thuê bao đầu kỳ * 100%, "
    "với ngưỡng cảnh báo < 95%."
)
SESSION_A_USER_MESSAGE = f"Tôi định nghĩa {FACT}"
SESSION_A_ASSISTANT_MESSAGE = "Đã ghi nhận định nghĩa KPI của bạn."
FOLLOW_UP = "Ngưỡng cảnh báo của chỉ số tôi đã định nghĩa là gì?"
REWRITTEN_FOLLOW_UP = "Ngưỡng cảnh báo của Tỷ lệ giữ chân là < 95%?"
EXPLICIT_OVERRIDE = "Bỏ ngưỡng cũ; dùng ngưỡng cảnh báo 90% cho Tỷ lệ giữ chân."
RECENT_OVERRIDE = "Trong phiên này, dùng ngưỡng cảnh báo 85% cho Tỷ lệ giữ chân."
RECENT_OVERRIDE_ACK = "Đã áp dụng ngưỡng 85% trong phiên này."
RECENT_FOLLOW_UP = "Ngưỡng đó là bao nhiêu?"
REWRITTEN_RECENT_FOLLOW_UP = "Ngưỡng cảnh báo của Tỷ lệ giữ chân là 85%?"
KIRA_ANSWER = "Synthetic KiRa answer"


class UnavailableMemory:
    async def search(self, *args: object, **kwargs: object):
        raise LongTermMemoryConnectionError

    async def process_memory(self, source: object):  # pragma: no cover - not used by /chat
        raise AssertionError("formation must not run in the online request path")


class RecallEmbedding:
    """Stable vector used to exercise real pgvector persistence and user filters."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(embedding_dims=3)

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        return [1.0, 0.0, 0.0]

    def embed_batch(
        self,
        texts: list[str],
        memory_action: str = "add",
    ) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class RecallMemoryLlm:
    """Return one native-V3 memory fact from the persisted Session A turn."""

    def generate_response(
        self,
        messages: list[dict[str, str]],
        response_format: object = None,
        **kwargs: object,
    ) -> str:
        assert response_format == {"type": "json_object"}
        prompt = messages[1]["content"]
        assert SESSION_A_USER_MESSAGE in prompt
        assert SESSION_A_ASSISTANT_MESSAGE in prompt
        return json.dumps(
            {"memory": [{"text": FACT, "attributed_to": "user"}]},
            ensure_ascii=False,
        )


def database_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return value


def settings(database: str, schema_name: str, user_id: str) -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="http://kira.test",
        kira_username="service-account",
        kira_basic_auth="credential",
        vllm_base_url="http://rewrite.test:8000",
        vllm_model="deterministic-rewriter",
        app_environment="test",
        dev_static_identity_enabled=True,
        dev_static_user_id=user_id,
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
        memory_operation_timeout_seconds=10,
        memory_search_timeout_seconds=5,
        memory_search_threshold=0,
    )


async def post_chat(app, session_id: str, message: str):
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client,
    ):
        response = await client.post(
            "/chat",
            json={"session_id": session_id, "message": message},
        )
        metrics = (await client.get("/metrics")).text
        ready = await client.get("/ready")
    assert response.status_code == 200
    assert response.content == b'data: {"text":"answer"}\n\n'
    assert ready.status_code == 200
    return metrics


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def assert_rewritten_threshold(query: str, expected: int, *forbidden: int) -> None:
    normalized_query = normalized(query)
    assert "tỷ lệ giữ chân" in normalized_query
    assert re.search(rf"(?<!\d){expected}\s*%", normalized_query)
    for threshold in forbidden:
        assert not re.search(rf"(?<!\d){threshold}\s*%", normalized_query)


async def test_session_a_formation_is_recalled_in_session_b_without_cross_user_leak() -> None:
    database = database_url()
    psycopg_dsn = normalize_psycopg_dsn(database)
    schema_name = f"memory_c4_{uuid4().hex}"
    user_a = f"c4-user-a-{uuid4().hex}"
    user_b = f"c4-user-b-{uuid4().hex}"
    session_a = f"c4-session-a-{uuid4().hex}"
    session_b = f"c4-session-b-{uuid4().hex}"
    session_c = f"c4-session-c-{uuid4().hex}"
    session_d = f"c4-session-d-{uuid4().hex}"
    resolved_settings = settings(database, schema_name, user_a)
    engine = create_async_engine(database)
    store = PostgresConversationStoreAdapter(engine)
    adapter: Mem0Adapter | None = None
    rewrite_envelopes: list[dict[str, object]] = []

    async def rewrite_handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        envelope = json.loads(request_payload["messages"][1]["content"])
        rewrite_envelopes.append(envelope)
        assert set(envelope) == {"long_term_memories", "recent_messages", "current_query"}
        assert envelope["long_term_memories"] == [FACT]
        if envelope["current_query"] == RECENT_FOLLOW_UP:
            assert [message["content"] for message in envelope["recent_messages"]] == [
                RECENT_OVERRIDE,
                RECENT_OVERRIDE_ACK,
            ]
        else:
            assert envelope["recent_messages"] == []
        output = {
            FOLLOW_UP: REWRITTEN_FOLLOW_UP,
            EXPLICIT_OVERRIDE: EXPLICIT_OVERRIDE,
            RECENT_FOLLOW_UP: REWRITTEN_RECENT_FOLLOW_UP,
        }[envelope["current_query"]]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": output}, "finish_reason": "stop"},
                ]
            },
        )

    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            psycopg_dsn,
            schema_name,
            resolved_settings.memory_collection_name,
            resolved_settings.memory_embedding_model,
            resolved_settings.memory_embedding_dims,
        )
        with (
            patch("mem0.memory.main.EmbedderFactory.create", return_value=RecallEmbedding()),
            patch("mem0.memory.main.LlmFactory.create", return_value=RecallMemoryLlm()),
            patch(
                "mem0.memory.main.extract_entities_batch",
                side_effect=lambda texts: [[] for _ in texts],
            ),
            patch("mem0.memory.main.extract_entities", return_value=[]),
        ):
            adapter = Mem0Adapter(
                create_mem0_client(resolved_settings),
                search_timeout_seconds=resolved_settings.memory_search_timeout_seconds,
                operation_timeout_seconds=resolved_settings.memory_operation_timeout_seconds,
            )
            turn_id = uuid4().hex
            formed_turn = await store.append_turn(
                user_a,
                ConversationMessage(
                    session_a,
                    turn_id,
                    ConversationRole.USER,
                    SESSION_A_USER_MESSAGE,
                    datetime(2026, 9, 8, tzinfo=UTC),
                ),
                ConversationMessage(
                    session_a,
                    turn_id,
                    ConversationRole.ASSISTANT,
                    SESSION_A_ASSISTANT_MESSAGE,
                    datetime(2026, 9, 8, 0, 0, 1, tzinfo=UTC),
                ),
            )
            formation = await ProcessMemoryUseCase(store, adapter).execute(formed_turn.reference)
            assert formation.events

            recalled = await adapter.search(user_a, FOLLOW_UP, top_k=10, threshold=0)
            assert len(recalled) == 1
            assert recalled[0].content == FACT
            assert recalled[0].metadata["conversation_id"] == str(
                formed_turn.reference.conversation_id
            )
            assert recalled[0].metadata["boundary_message_id"] == (
                formed_turn.reference.boundary_message_id
            )
            assert await adapter.search(user_b, FOLLOW_UP, top_k=10, threshold=0) == ()

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(rewrite_handler)
            ) as rewrite_http:
                rewriter = VllmQueryRewriterAdapter(rewrite_http, resolved_settings)

                kira_a = FakeKiraClient(events=[kira_event('{"text":"answer"}', KIRA_ANSWER)])
                app_a = create_app(
                    settings=resolved_settings,
                    kira_client=kira_a,
                    conversation_store=store,
                    query_rewriter=rewriter,
                    long_term_memory=adapter,
                )
                metrics_a = await post_chat(app_a, session_b, FOLLOW_UP)
                assert kira_a.messages == [REWRITTEN_FOLLOW_UP]
                assert 'kira_memory_search_total{outcome="success"} 1.0' in metrics_a
                assert "kira_memory_search_results_sum 1.0" in metrics_a

                persisted_b = await store.read_recent(user_a, session_b, 10)
                assert [message.content for message in persisted_b] == [FOLLOW_UP, KIRA_ANSWER]

                kira_b = FakeKiraClient(events=[kira_event('{"text":"answer"}', KIRA_ANSWER)])
                app_b = create_app(
                    settings=settings(database, schema_name, user_b),
                    kira_client=kira_b,
                    conversation_store=store,
                    query_rewriter=rewriter,
                    long_term_memory=adapter,
                )
                metrics_b = await post_chat(app_b, session_b, FOLLOW_UP)
                assert kira_b.messages == [FOLLOW_UP]
                assert 'kira_memory_search_total{outcome="success"} 1.0' in metrics_b
                assert "kira_memory_search_results_sum 0.0" in metrics_b

                kira_override = FakeKiraClient(
                    events=[kira_event('{"text":"answer"}', KIRA_ANSWER)]
                )
                app_override = create_app(
                    settings=resolved_settings,
                    kira_client=kira_override,
                    conversation_store=store,
                    query_rewriter=rewriter,
                    long_term_memory=adapter,
                )
                await post_chat(app_override, session_c, EXPLICIT_OVERRIDE)
                assert kira_override.messages == [EXPLICIT_OVERRIDE]

                recent_turn_id = uuid4().hex
                await store.append_turn(
                    user_a,
                    ConversationMessage(
                        session_d,
                        recent_turn_id,
                        ConversationRole.USER,
                        RECENT_OVERRIDE,
                        datetime(2026, 9, 8, 0, 1, tzinfo=UTC),
                    ),
                    ConversationMessage(
                        session_d,
                        recent_turn_id,
                        ConversationRole.ASSISTANT,
                        RECENT_OVERRIDE_ACK,
                        datetime(2026, 9, 8, 0, 1, 1, tzinfo=UTC),
                    ),
                )
                kira_recent = FakeKiraClient(events=[kira_event('{"text":"answer"}', KIRA_ANSWER)])
                app_recent = create_app(
                    settings=resolved_settings,
                    kira_client=kira_recent,
                    conversation_store=store,
                    query_rewriter=rewriter,
                    long_term_memory=adapter,
                )
                await post_chat(app_recent, session_d, RECENT_FOLLOW_UP)
                assert kira_recent.messages == [REWRITTEN_RECENT_FOLLOW_UP]

                kira_fallback = FakeKiraClient(
                    events=[kira_event('{"text":"answer"}', KIRA_ANSWER)]
                )
                app_fallback = create_app(
                    settings=resolved_settings,
                    kira_client=kira_fallback,
                    conversation_store=store,
                    query_rewriter=rewriter,
                    long_term_memory=UnavailableMemory(),
                )
                fallback_metrics = await post_chat(
                    app_fallback,
                    f"c4-session-fallback-{uuid4().hex}",
                    FOLLOW_UP,
                )
                assert kira_fallback.messages == [FOLLOW_UP]
                assert (
                    'kira_context_degraded_total{dependency="mem0",operation="memory_search"} 1.0'
                ) in fallback_metrics

            combined_metrics = metrics_a + metrics_b + fallback_metrics
            for sensitive_value in (
                user_a,
                user_b,
                session_a,
                session_b,
                FACT,
                FOLLOW_UP,
            ):
                assert sensitive_value not in combined_metrics

            assert [envelope["current_query"] for envelope in rewrite_envelopes] == [
                FOLLOW_UP,
                EXPLICIT_OVERRIDE,
                RECENT_FOLLOW_UP,
            ]

        with psycopg.connect(psycopg_dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(resolved_settings.memory_collection_name),
                )
            )
            assert cursor.fetchone() == (1,)
    finally:
        if adapter is not None:
            adapter.close()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    delete(conversations).where(conversations.c.user_id.in_((user_a, user_b)))
                )
        finally:
            await engine.dispose()
            with psycopg.connect(psycopg_dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
                )


@pytest.mark.memory_llm_integration
async def test_live_cross_session_memory_and_rewrite_gate() -> None:
    """Opt-in semantic gate using configured embedding, memory-LLM, and rewrite endpoints."""
    if os.environ.get("RUN_CROSS_SESSION_EVAL") != "1":
        pytest.skip("set RUN_CROSS_SESSION_EVAL=1 to run the real cross-session gate")

    database = os.environ.get("POSTGRES_TEST_URL")
    assert database, "POSTGRES_TEST_URL is required when RUN_CROSS_SESSION_EVAL=1"
    psycopg_dsn = normalize_psycopg_dsn(database)
    schema_name = f"memory_c4_live_{uuid4().hex}"
    user_a = f"c4-live-user-a-{uuid4().hex}"
    user_b = f"c4-live-user-b-{uuid4().hex}"
    session_a = f"c4-live-session-a-{uuid4().hex}"
    session_recent = f"c4-live-session-recent-{uuid4().hex}"
    live_settings = Settings(
        ltm_enabled=True,
        memory_database_url=database,
        memory_admin_database_url=database,
        memory_schema=schema_name,
    )
    engine = create_async_engine(database)
    store = PostgresConversationStoreAdapter(engine)
    memory: Mem0Adapter | None = None

    try:
        await initialize_memory_schema(live_settings)
        memory = Mem0Adapter.from_settings(live_settings)
        turn_id = uuid4().hex
        completed = await store.append_turn(
            user_a,
            ConversationMessage(
                session_a,
                turn_id,
                ConversationRole.USER,
                SESSION_A_USER_MESSAGE,
                datetime(2026, 9, 8, tzinfo=UTC),
            ),
            ConversationMessage(
                session_a,
                turn_id,
                ConversationRole.ASSISTANT,
                SESSION_A_ASSISTANT_MESSAGE,
                datetime(2026, 9, 8, 0, 0, 1, tzinfo=UTC),
            ),
        )
        formation = await ProcessMemoryUseCase(store, memory).execute(completed.reference)
        assert formation.events

        recalled = await memory.search(
            user_a,
            FOLLOW_UP,
            top_k=live_settings.memory_search_top_k,
            threshold=live_settings.memory_search_threshold,
        )
        formula_case = next(
            case for case in CASES if case.name == "user_defined_metric_formula_exact"
        )
        assert score_case(formula_case, tuple(item.content for item in recalled)).passed
        assert (
            await memory.search(
                user_b,
                FOLLOW_UP,
                top_k=live_settings.memory_search_top_k,
                threshold=live_settings.memory_search_threshold,
            )
            == ()
        )

        context_builder = ContextBuilder(
            max_recent_messages=live_settings.max_recent_messages,
            recent_token_budget=live_settings.recent_context_token_budget,
            max_long_term_memories=live_settings.memory_search_top_k,
        )
        async with httpx.AsyncClient() as rewrite_http:
            rewriter = VllmQueryRewriterAdapter(rewrite_http, live_settings)

            session_b_recent = await store.read_recent(user_a, f"session-b-{uuid4().hex}", 10)
            assert session_b_recent == ()
            rewritten = await rewriter.rewrite(
                context_builder.build(session_b_recent, FOLLOW_UP, recalled)
            )
            assert_rewritten_threshold(rewritten, 95)

            current_override = await rewriter.rewrite(
                context_builder.build((), EXPLICIT_OVERRIDE, recalled)
            )
            assert_rewritten_threshold(current_override, 90, 95)

            recent_turn_id = uuid4().hex
            await store.append_turn(
                user_a,
                ConversationMessage(
                    session_recent,
                    recent_turn_id,
                    ConversationRole.USER,
                    RECENT_OVERRIDE,
                    datetime(2026, 9, 8, 0, 1, tzinfo=UTC),
                ),
                ConversationMessage(
                    session_recent,
                    recent_turn_id,
                    ConversationRole.ASSISTANT,
                    RECENT_OVERRIDE_ACK,
                    datetime(2026, 9, 8, 0, 1, 1, tzinfo=UTC),
                ),
            )
            recent = await store.read_recent(
                user_a,
                session_recent,
                live_settings.max_recent_messages,
            )
            recent_override = await rewriter.rewrite(
                context_builder.build(recent, RECENT_FOLLOW_UP, recalled)
            )
            assert_rewritten_threshold(recent_override, 85, 95)
    finally:
        if memory is not None:
            memory.close()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    delete(conversations).where(conversations.c.user_id.in_((user_a, user_b)))
                )
        finally:
            await engine.dispose()
            with psycopg.connect(psycopg_dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
                )
