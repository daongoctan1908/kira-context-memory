import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.services.memory_policy import MEMORY_EXTRACTION_INSTRUCTIONS
from app.application.services.memory_temporal import (
    TEMPORAL_GUIDANCE,
    build_memory_extraction_prompt,
)
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
from app.domain.models.memory import MemoryLifecycleEvent, MemorySource
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
        uuid4(),
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
    assert config["custom_instructions"] == MEMORY_EXTRACTION_INSTRUCTIONS
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
        assert client.custom_instructions == MEMORY_EXTRACTION_INSTRUCTIONS
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

    assert result.events == (MemoryLifecycleEvent("ADD", "memory-1", "preference"),)
    assert result.added_memory_ids == ("memory-1",)
    messages, kwargs = client.add_calls[0]
    assert messages == [
        {"role": "user", "content": "Tôi thích bảng"},
        {"role": "assistant", "content": "Đã rõ"},
    ]
    assert kwargs["user_id"] == "user-1"
    assert kwargs["metadata"]["formation_event_id"] == str(memory_source.formation_event_id)
    assert kwargs["metadata"]["boundary_message_id"] == 42
    assert kwargs["infer"] is True
    assert kwargs["prompt"] == build_memory_extraction_prompt(memory_source.messages)
    assert "timestamp" not in kwargs
    assert "expiration_date" not in kwargs
    assert not hasattr(client, "update")
    assert not hasattr(client, "delete")


async def test_concurrent_formations_keep_request_local_timestamps_and_raw_content():
    class ConcurrentMem0(FakeMem0):
        custom_instructions = MEMORY_EXTRACTION_INSTRUCTIONS

        def __init__(self):
            super().__init__()
            self.ready = asyncio.Event()

        async def add(self, messages, **kwargs):
            self.add_calls.append((messages, kwargs))
            if len(self.add_calls) == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=1)
            return {"results": []}

    client = ConcurrentMem0()
    memory_adapter = adapter(client)
    first = source()
    second = replace(
        first,
        formation_event_id=uuid4(),
        messages=tuple(
            replace(message, timestamp=datetime(2026, 10, 1, tzinfo=UTC))
            for message in first.messages
        ),
    )
    await asyncio.gather(
        memory_adapter.process_memory(first), memory_adapter.process_memory(second)
    )
    calls = {
        kwargs["metadata"]["formation_event_id"]: (messages, kwargs)
        for messages, kwargs in client.add_calls
    }
    for item in (first, second):
        messages, kwargs = calls[str(item.formation_event_id)]
        assert messages == [
            {"role": message.role.value, "content": message.content} for message in item.messages
        ]
        assert kwargs["prompt"] == build_memory_extraction_prompt(item.messages)
    assert client.custom_instructions == MEMORY_EXTRACTION_INSTRUCTIONS


async def test_replayed_source_produces_identical_prompt_and_preserves_event_identity():
    client = FakeMem0()
    memory_adapter = adapter(client)
    item = source()
    await memory_adapter.process_memory(item)
    await memory_adapter.process_memory(item)
    assert client.add_calls[0] == client.add_calls[1]


async def test_from_settings_uses_utc_source_timestamps(monkeypatch):
    client = FakeMem0()
    monkeypatch.setattr(
        "app.infrastructure.memory.mem0_adapter.create_mem0_client", lambda _: client
    )
    memory_adapter = Mem0Adapter.from_settings(settings())
    await memory_adapter.process_memory(source())
    prompt = client.add_calls[0][1]["prompt"]
    table = json.loads(prompt.split(TEMPORAL_GUIDANCE, 1)[1].strip())
    assert set(table) == {"source_time"}
    assert table["source_time"][0].endswith("+00:00")


async def test_process_memory_preserves_ordered_lifecycle_actions():
    client = FakeMem0(
        add_response={
            "results": [
                {"id": "memory-1", "memory": "new", "event": "ADD"},
                {"id": "memory-1", "memory": "updated", "event": "UPDATE"},
                {"id": "memory-2", "event": "DELETE"},
                {"event": "NONE"},
                {"id": "memory-3", "event": "ARCHIVE", "provider_field": "ignored"},
            ]
        }
    )

    result = await adapter(client).process_memory(source())

    assert result.events == (
        MemoryLifecycleEvent("ADD", "memory-1", "new"),
        MemoryLifecycleEvent("UPDATE", "memory-1", "updated"),
        MemoryLifecycleEvent("DELETE", "memory-2"),
        MemoryLifecycleEvent("NONE"),
        MemoryLifecycleEvent("ARCHIVE", "memory-3"),
    )
    assert result.added_memory_ids == ("memory-1",)


async def test_process_memory_accepts_empty_results():
    result = await adapter(FakeMem0(add_response={"results": []})).process_memory(source())

    assert result.events == ()
    assert result.added is False


@pytest.mark.parametrize(
    "response",
    [
        "not-a-response",
        {"unexpected": []},
        {"results": {}},
        {"results": ["not-a-row"]},
        {"results": [{}]},
        {"results": [{"event": ""}]},
        {"results": [{"event": 42}]},
        {"results": [{"event": "ADD", "id": ""}]},
        {"results": [{"event": "ADD", "memory": " "}]},
        {"results": [{"event": "DELETE", "id": 42}]},
        {"results": [{"event": "UPDATE", "memory": []}]},
    ],
)
async def test_process_memory_rejects_malformed_lifecycle_rows(response):
    client = FakeMem0(add_response=response)

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
