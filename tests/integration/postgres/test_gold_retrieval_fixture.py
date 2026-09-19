"""Gold fixture through native Mem0, real pgvector, and the runtime retrieval adapter."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.settings import Settings
from app.infrastructure.memory.mem0_adapter import Mem0Adapter, create_mem0_client
from app.infrastructure.memory.postgres_admin import (
    _initialize_memory_schema_sync,
    normalize_psycopg_dsn,
)
from app.infrastructure.postgres.conversation_store import PostgresConversationStoreAdapter
from evaluation.compiler import compile_dataset
from evaluation.isolation import create_isolation_plan
from evaluation.models import Outcome, Profile, RetrievalInput
from evaluation.retrieval import GoldRetrievalFixtureManager, RetrievalEvaluator

pytestmark = pytest.mark.postgres_integration


class FixtureEmbedding:
    def __init__(self) -> None:
        self.config = SimpleNamespace(embedding_dims=3)

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        return [1.0, 0.0, 0.0]


class ExtractionMustNotRun:
    def generate_response(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("gold fixture must bypass extraction")


def _database_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    return value


def _settings(database_url: str, schema_name: str, collection_name: str) -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="http://kira.test",
        kira_username="service-account",
        kira_basic_auth="credential",
        ltm_enabled=True,
        memory_database_url=database_url,
        memory_schema=schema_name,
        memory_collection_name=collection_name,
        memory_postgres_min_connections=1,
        memory_postgres_max_connections=3,
        memory_embedding_base_url="http://deterministic-embedding.test",
        memory_embedding_model="deterministic-embedding-v1",
        memory_embedding_dims=3,
        memory_llm_base_url="http://unused-extraction.test",
        memory_llm_model="unused-extraction-v1",
        memory_operation_timeout_seconds=10,
        memory_search_timeout_seconds=5,
    )


async def test_gold_fixture_uses_native_pgvector_retrieval_and_exact_cleanup() -> None:
    database_url = _database_url()
    plan = create_isolation_plan(
        run_id=uuid4(),
        owner_token=uuid4(),
        conversation_database_url=database_url,
        memory_database_url=database_url,
    )
    settings = _settings(database_url, plan.memory_schema, plan.memory_collection)
    psycopg_dsn = normalize_psycopg_dsn(database_url)
    client = None
    adapter = None
    engine = create_async_engine(database_url)
    try:
        await asyncio.to_thread(
            _initialize_memory_schema_sync,
            psycopg_dsn,
            plan.memory_schema,
            plan.memory_collection,
            settings.memory_embedding_model,
            settings.memory_embedding_dims,
        )
        source = next(
            case
            for case in compile_dataset(seed=743).cases
            if isinstance(case.inputs, RetrievalInput) and len(case.inputs.memories) >= 2
        )
        case = source.model_copy(
            update={
                "inputs": source.inputs.model_copy(update={"memories": source.inputs.memories[:2]}),
                "gold": source.gold.model_copy(
                    update={
                        "relevant_memory_ids": tuple(
                            memory.gold_id for memory in source.inputs.memories[:2]
                        )
                    }
                ),
            }
        )
        with (
            patch("mem0.memory.main.EmbedderFactory.create", return_value=FixtureEmbedding()),
            patch("mem0.memory.main.LlmFactory.create", return_value=ExtractionMustNotRun()),
        ):
            store = PostgresConversationStoreAdapter(engine)
            await store.validate_schema()
            client = create_mem0_client(settings)
            manager = GoldRetrievalFixtureManager(client, plan, store)
            fixture = await manager.setup((case,))
            adapter = Mem0Adapter(
                client,
                search_timeout_seconds=5,
                operation_timeout_seconds=10,
            )
            result = await RetrievalEvaluator(
                adapter,
                profile=Profile.INTERNAL_TEST,
                backend="native",
            ).evaluate_gold_fixture(
                case,
                fixture,
            )
            assert result.outcome is Outcome.PASS
            assert result.score is not None and result.score.recall_at_3 == 1
            assert set(result.returned_memory_ids) == {
                memory.memory_id for memory in fixture.memories
            }

            await manager.cleanup(fixture)
            assert (
                await adapter.search(
                    fixture.persisted_user_id(case.inputs.user_id),
                    case.inputs.current_query,
                    top_k=10,
                    threshold=0,
                )
                == ()
            )
    finally:
        await engine.dispose()
        if adapter is not None:
            adapter.close()
        elif client is not None:
            client.close()
        with psycopg.connect(psycopg_dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(plan.memory_schema)
                )
            )
