"""Characterize the pristine Mem0 v2.0.20 V3 ADD-only pipeline."""

import hashlib
import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import mem0
from mem0 import Memory
from mem0.configs.base import MemoryConfig


@dataclass(slots=True)
class StoredVector:
    """Minimum vector-store result shape consumed by Mem0."""

    id: str
    payload: dict[str, object]
    score: float = 1.0


def test_internal_distribution_preserves_upstream_namespace() -> None:
    assert mem0.__version__ == "2.0.20+viettel.1"
    assert Memory.__module__ == "mem0.memory.main"


def test_v3_add_only_duplicate_and_user_scope_behavior() -> None:
    fact = "The user prefers revenue figures in billions of VND."
    another_fact = "The user prefers monthly comparisons."
    extracted = [fact, fact, another_fact, fact]
    stored: list[StoredVector] = []

    vector_store = MagicMock()

    def search(*, filters: dict[str, str], **_: object) -> list[StoredVector]:
        return [row for row in stored if row.payload.get("user_id") == filters["user_id"]]

    def insert(
        *,
        ids: list[str],
        payloads: list[dict[str, object]],
        **_: object,
    ) -> None:
        stored.extend(
            StoredVector(memory_id, payload)
            for memory_id, payload in zip(ids, payloads, strict=True)
        )

    vector_store.search.side_effect = search
    vector_store.insert.side_effect = insert

    embedder = MagicMock()
    embedder.config = MagicMock(embedding_dims=3)
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    embedder.embed_batch.return_value = [[0.4, 0.5, 0.6]]

    llm = MagicMock()
    llm.generate_response.side_effect = [
        json.dumps({"memory": [{"text": item}]}) for item in extracted
    ]

    history = MagicMock()
    history.get_last_messages.return_value = []

    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.extract_entities_batch", return_value=[[]]),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=vector_store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        memory = Memory(MemoryConfig())
        first = memory.add("first turn", user_id="user-a", infer=True)
        duplicate = memory.add("repeat turn", user_id="user-a", infer=True)
        distinct = memory.add("new turn", user_id="user-a", infer=True)
        other_user = memory.add("other user turn", user_id="user-b", infer=True)

    assert [item["event"] for item in first["results"]] == ["ADD"]
    assert duplicate == {"results": []}
    assert [item["event"] for item in distinct["results"]] == ["ADD"]
    assert [item["event"] for item in other_user["results"]] == ["ADD"]
    assert [row.payload["user_id"] for row in stored] == ["user-a", "user-a", "user-b"]
    assert stored[0].payload["hash"] == hashlib.md5(fact.encode()).hexdigest()
    assert vector_store.insert.call_count == 3
    vector_store.update.assert_not_called()
    vector_store.delete.assert_not_called()

    search_filters = [call.kwargs["filters"] for call in vector_store.search.call_args_list]
    assert search_filters == [
        {"user_id": "user-a"},
        {"user_id": "user-a"},
        {"user_id": "user-a"},
        {"user_id": "user-b"},
    ]
