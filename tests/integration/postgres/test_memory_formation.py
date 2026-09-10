"""Real PostgreSQL/pgvector formation gate with deterministic provider doubles."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS
from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.config.settings import Settings
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.memory.mem0_adapter import Mem0Adapter, create_mem0_client
from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    formation_receipt_table_name,
    normalize_psycopg_dsn,
)
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from app.infrastructure.postgres.schema import conversations
from scripts.check_live_memory_policy import score_case
from tests.support.memory_policy_cases import CASES, MemoryPolicyCase

pytestmark = pytest.mark.postgres_integration

POSITIVE_CASES = (
    "user_context_explicit_scope",
    "analysis_preference_explicit",
    "user_defined_metric_formula_exact",
    "user_defined_convention_mtd",
    "temporary_focus_with_explicit_window",
    "episodic_context_user_confirmed",
)
NEGATIVE_CASES = (
    "greeting_only",
    "ordinary_query_entity",
    "assistant_generated_kpi_result",
    "assistant_guess_about_preference",
    "synthetic_secret",
    "conversation_prompt_injection",
)
SELECTED_CASES = tuple(
    case for case in CASES if case.name in frozenset((*POSITIVE_CASES, *NEGATIVE_CASES))
)

FACTS_BY_CASE: dict[str, str] = {
    "user_context_explicit_scope": (
        "Người dùng phụ trách vận hành chất lượng mạng di động tại khu vực miền Bắc."
    ),
    "analysis_preference_explicit": (
        "Trong mọi phân tích, người dùng muốn so sánh theo tháng và trình bày dạng bảng."
    ),
    "user_defined_metric_formula_exact": (
        "Tỷ lệ giữ chân = (thuê bao cuối kỳ - thuê bao mới) / thuê bao đầu kỳ * 100%, "
        "với ngưỡng cảnh báo < 95%."
    ),
    "user_defined_convention_mtd": (
        "MTD luôn có nghĩa là từ ngày đầu tháng đến ngày dữ liệu gần nhất."
    ),
    "temporary_focus_with_explicit_window": (
        "Từ 01/09/2026 đến hết 30/09/2026, người dùng ưu tiên theo dõi tỷ lệ rớt "
        "cuộc gọi tại Hà Nội."
    ),
    "episodic_context_user_confirmed": (
        "Kết luận đợt phân tích ngày 05/09/2026: suy giảm tập trung ở nhóm trả trước miền Bắc."
    ),
}


class DeterministicEmbedding:
    """Stable non-zero vectors; isolation is tested through real pgvector filters."""

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


class DeterministicMemoryLlm:
    """Return fixed native-V3 envelopes while asserting the production policy is present."""

    def __init__(self) -> None:
        self.case_names: list[str] = []

    def generate_response(
        self,
        messages: list[dict[str, str]],
        response_format: object = None,
        **kwargs: object,
    ) -> str:
        assert response_format == {"type": "json_object"}
        assert len(messages) == 2
        prompt = messages[1]["content"]
        assert MEMORY_EXTRACTION_INSTRUCTIONS in prompt
        matched = [case for case in SELECTED_CASES if case.messages[0].content in prompt]
        if len(matched) != 1:
            raise AssertionError("deterministic memory LLM could not identify exactly one case")
        case = matched[0]
        self.case_names.append(case.name)
        fact = FACTS_BY_CASE.get(case.name)
        memory = [] if fact is None else [{"text": fact, "attributed_to": "user"}]
        return json.dumps({"memory": memory}, ensure_ascii=False)


def _database_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return value


def _settings(database_url: str, schema_name: str) -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="http://kira.test",
        kira_username="service-account",
        kira_basic_auth="credential",
        ltm_enabled=True,
        memory_database_url=database_url,
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
    )


async def _persist_case(
    store: PostgresConversationStoreAdapter,
    case: MemoryPolicyCase,
    user_id: str,
    session_id: str,
):
    if len(case.messages) % 2:
        raise AssertionError("B4 cases must be representable as completed conversation turns")
    reference = None
    timestamp = datetime(2026, 9, 7, tzinfo=UTC)
    for index in range(0, len(case.messages), 2):
        user_source, assistant_source = case.messages[index : index + 2]
        if user_source.role != "user" or assistant_source.role != "assistant":
            raise AssertionError("B4 cases must contain ordered user/assistant pairs")
        turn_id = uuid4().hex
        result = await store.append_turn(
            user_id,
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.USER,
                user_source.content,
                timestamp + timedelta(seconds=index),
            ),
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.ASSISTANT,
                assistant_source.content,
                timestamp + timedelta(seconds=index + 1),
            ),
        )
        assert result.inserted is True
        reference = result.reference
    assert reference is not None
    return reference


async def test_exact_boundary_formation_policy_cases_dedup_and_user_isolation() -> None:
    database_url = _database_url()
    psycopg_dsn = normalize_psycopg_dsn(database_url)
    schema_name = f"memory_b4_{uuid4().hex}"
    settings = _settings(database_url, schema_name)
    engine = create_async_engine(database_url)
    store = PostgresConversationStoreAdapter(engine)
    llm = DeterministicMemoryLlm()
    adapter: Mem0Adapter | None = None
    created_users: list[str] = []
    references = {}
    formation_event_ids = {}

    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            psycopg_dsn,
            schema_name,
            settings.memory_collection_name,
            settings.memory_embedding_model,
            settings.memory_embedding_dims,
        )
        with (
            patch(
                "mem0.memory.main.EmbedderFactory.create",
                return_value=DeterministicEmbedding(),
            ),
            patch("mem0.memory.main.LlmFactory.create", return_value=llm),
            patch(
                "mem0.memory.main.extract_entities_batch",
                side_effect=lambda texts: [[] for _ in texts],
            ),
            patch("mem0.memory.main.extract_entities", return_value=[]),
        ):
            adapter = Mem0Adapter(
                create_mem0_client(settings),
                search_timeout_seconds=settings.memory_search_timeout_seconds,
                operation_timeout_seconds=settings.memory_operation_timeout_seconds,
            )
            process_memory = ProcessMemoryUseCase(
                store,
                adapter,
                message_limit=settings.memory_formation_message_limit,
            )

            for case in SELECTED_CASES:
                user_id = f"b4-user-{case.name}-{uuid4().hex}"
                other_user_id = f"b4-other-{uuid4().hex}"
                session_id = f"b4-session-{uuid4().hex}"
                created_users.append(user_id)
                reference = await _persist_case(store, case, user_id, session_id)
                references[case.name] = reference
                formation_event_id = uuid4()
                formation_event_ids[case.name] = formation_event_id

                result = await process_memory.execute(reference, formation_event_id)
                found = await adapter.search(
                    user_id,
                    "durable profile preference and convention",
                    top_k=10,
                    threshold=0,
                )
                score = score_case(case, tuple(memory.content for memory in found))
                assert score.passed, (case.name, score.reason_codes)
                assert (
                    await adapter.search(
                        other_user_id,
                        "durable profile preference and convention",
                        top_k=10,
                        threshold=0,
                    )
                    == ()
                )

                if case.expectation.should_extract:
                    assert result.events
                    assert len(found) == 1
                    assert found[0].metadata["formation_event_id"] == str(formation_event_id)
                    assert found[0].metadata["conversation_id"] == str(reference.conversation_id)
                    assert found[0].metadata["turn_id"] == reference.turn_id
                    assert found[0].metadata["boundary_message_id"] == reference.boundary_message_id
                else:
                    assert result.events == ()
                    assert found == ()

            formula_reference = references["user_defined_metric_formula_exact"]
            llm_calls_before_retry = len(llm.case_names)
            with patch.object(
                adapter._client.vector_store,  # type: ignore[attr-defined]
                "search",
                side_effect=AssertionError("receipt retry must bypass semantic top_k retrieval"),
            ):
                duplicate = await process_memory.execute(
                    formula_reference,
                    formation_event_ids["user_defined_metric_formula_exact"],
                )
            formula_memories = await adapter.search(
                formula_reference.user_id,
                "Tỷ lệ giữ chân",
                top_k=10,
                threshold=0,
            )
            assert len(duplicate.events) == 1
            assert duplicate.events[0].action == "ADD"
            assert len(llm.case_names) == llm_calls_before_retry
            assert len(formula_memories) == 1
            assert (
                "Tỷ lệ giữ chân = (thuê bao cuối kỳ - thuê bao mới) / "
                "thuê bao đầu kỳ * 100%" in formula_memories[0].content
            )
            assert "< 95%" in formula_memories[0].content

        with psycopg.connect(psycopg_dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(settings.memory_collection_name),
                )
            )
            assert cursor.fetchone() == (len(POSITIVE_CASES),)
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(formation_receipt_table_name(settings.memory_collection_name)),
                )
            )
            assert cursor.fetchone() == (len(SELECTED_CASES),)
        assert set(POSITIVE_CASES).issubset(llm.case_names)
        assert set(NEGATIVE_CASES).issubset(llm.case_names)
    finally:
        if adapter is not None:
            adapter.close()
        try:
            if created_users:
                async with engine.begin() as connection:
                    await connection.execute(
                        delete(conversations).where(conversations.c.user_id.in_(created_users))
                    )
        finally:
            await engine.dispose()
            with psycopg.connect(psycopg_dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
                )
