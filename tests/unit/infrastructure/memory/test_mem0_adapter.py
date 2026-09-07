import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.config.settings import Settings
from app.domain.errors.memory import (
    LongTermMemoryConnectionError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import MemorySource
from app.infrastructure.memory.mem0_adapter import (
    Mem0Adapter,
    build_mem0_config,
    create_mem0_client,
)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="http://kira.test",
        kira_username="service-account",
        kira_basic_auth="credential",
        ltm_enabled=True,
        memory_database_url="postgresql+asyncpg://user:secret@db/memory",
        memory_embedding_base_url="http://embed.test",
        memory_embedding_model="viettel-embedding",
        memory_embedding_api_key="embed-secret",
        memory_embedding_dims=4,
        memory_llm_base_url="http://memory-llm.test/v1",
        memory_llm_model="viettel-memory-llm",
        memory_llm_api_key="llm-secret",
    )


class FakeMem0:
    def __init__(self, *, search_response=None, add_response=None, error=None):
        self.search_response = search_response or {"results": []}
        self.add_response = add_response or {"results": []}
        self.error = error
        self.search_calls = []
        self.add_calls = []
        self.closed = False

    async def search(self, query, **kwargs):
        self.search_calls.append((query, kwargs))
        if self.error:
            raise self.error
        return self.search_response

    async def add(self, messages, **kwargs):
        self.add_calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return self.add_response

    def close(self):
        self.closed = True


def source() -> MemorySource:
    timestamp = datetime(2026, 9, 6, tzinfo=UTC)
    reference = CompletedTurnReference("user-1", "session-1", uuid4(), "turn-1", 42)
    return MemorySource(
        reference,
        (
            ConversationMessage(
                "session-1", "turn-1", ConversationRole.USER, "Tôi thích bảng", timestamp
            ),
            ConversationMessage(
                "session-1", "turn-1", ConversationRole.ASSISTANT, "Đã rõ", timestamp
            ),
        ),
    )


def adapter(client, *, search_timeout=1, operation_timeout=1):
    return Mem0Adapter(
        client,
        search_timeout_seconds=search_timeout,
        operation_timeout_seconds=operation_timeout,
    )


def test_build_config_pins_internal_endpoints_and_forbids_runtime_ddl():
    config = build_mem0_config(settings())

    assert config["history_db_path"] == ":memory:"
    vector = config["vector_store"]["config"]
    assert vector["schema_name"] == "memory"
    assert vector["auto_create"] is False
    assert vector["connection_string"].startswith("postgresql://")
    assert config["embedder"]["config"]["model"] == "viettel-embedding"
    assert config["llm"]["config"]["model"] == "viettel-memory-llm"


def test_internal_mem0_package_constructs_without_database_ddl():
    client = create_mem0_client(settings())
    pool = client.vector_store.connection_pool
    try:
        assert type(client).__name__ == "AsyncMemory"
        assert client.vector_store.schema_name == "memory"
        assert client.vector_store.auto_create is False
        assert client.vector_store._collection_ensured is False
    finally:
        adapter(client).close()
    assert pool.closed is True


async def test_search_is_user_scoped_and_maps_valid_results():
    client = FakeMem0(
        search_response={
            "results": [
                {
                    "id": "memory-1",
                    "memory": "Người dùng thích bảng",
                    "score": 0.8,
                    "user_id": "user-1",
                    "metadata": {"turn_id": "turn-1"},
                }
            ]
        }
    )

    result = await adapter(client).search("user-1", "định dạng ưa thích", top_k=5, threshold=0.2)

    assert result[0].memory_id == "memory-1"
    assert client.search_calls == [
        (
            "định dạng ưa thích",
            {
                "top_k": 5,
                "threshold": 0.2,
                "filters": {"user_id": "user-1"},
                "rerank": False,
            },
        )
    ]


async def test_search_rejects_cross_user_result_even_if_provider_misbehaves():
    client = FakeMem0(
        search_response={
            "results": [
                {
                    "id": "memory-1",
                    "memory": "private",
                    "score": 0.9,
                    "user_id": "other-user",
                }
            ]
        }
    )

    with pytest.raises(LongTermMemoryProtocolError):
        await adapter(client).search("user-1", "query", top_k=5, threshold=0.1)


async def test_process_memory_calls_only_add_with_exact_boundary_metadata():
    client = FakeMem0(
        add_response={"results": [{"id": "memory-1", "memory": "preference", "event": "ADD"}]}
    )
    memory_source = source()

    result = await adapter(client).process_memory(memory_source)

    assert result.added_memory_ids == ("memory-1",)
    messages, kwargs = client.add_calls[0]
    assert messages == [
        {"role": "user", "content": "Tôi thích bảng"},
        {"role": "assistant", "content": "Đã rõ"},
    ]
    assert kwargs["user_id"] == "user-1"
    assert kwargs["metadata"]["boundary_message_id"] == 42
    assert kwargs["infer"] is True
    assert not hasattr(client, "update")
    assert not hasattr(client, "delete")


async def test_process_memory_rejects_non_add_event():
    client = FakeMem0(add_response={"results": [{"id": "memory-1", "event": "UPDATE"}]})

    with pytest.raises(LongTermMemoryProtocolError):
        await adapter(client).process_memory(source())


@pytest.mark.parametrize(
    "error,expected",
    [
        (ConnectionError("private endpoint"), LongTermMemoryConnectionError),
        (TimeoutError(), LongTermMemoryTimeoutError),
    ],
)
async def test_provider_failures_are_sanitized(error, expected):
    with pytest.raises(expected):
        await adapter(FakeMem0(error=error)).search("user-1", "query", top_k=5, threshold=0.1)


async def test_adapter_deadline_maps_to_timeout():
    class HangingMem0(FakeMem0):
        async def search(self, query, **kwargs):
            await asyncio.Event().wait()

    with pytest.raises(LongTermMemoryTimeoutError):
        await adapter(HangingMem0(), search_timeout=0.01).search(
            "user-1", "query", top_k=5, threshold=0.1
        )


def test_close_releases_mem0_client():
    client = FakeMem0()
    adapter(client).close()
    assert client.closed is True
