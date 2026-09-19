"""Characterize the pristine Mem0 v2.0.20 V3 ADD-only pipeline."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import mem0
from mem0 import AsyncMemory, Memory
from mem0.configs.base import MemoryConfig

from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import MemorySource
from app.infrastructure.memory.mem0_adapter import Mem0Adapter


@dataclass(slots=True)
class StoredVector:
    """Minimum vector-store result shape consumed by Mem0."""

    id: str
    payload: dict[str, object]
    score: float = 1.0


def _memory_source(
    *,
    user_id: str,
    conversation_id: UUID,
    session_id: str,
    turn_id: str,
    boundary_message_id: int,
    formation_event_id: UUID,
) -> MemorySource:
    timestamp = datetime(2026, 9, 19, 2, tzinfo=UTC)
    return MemorySource(
        CompletedTurnReference(
            user_id,
            session_id,
            conversation_id,
            turn_id,
            boundary_message_id,
        ),
        (
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.USER,
                f"Question for {turn_id}",
                timestamp,
            ),
            ConversationMessage(
                session_id,
                turn_id,
                ConversationRole.ASSISTANT,
                f"Answer for {turn_id}",
                timestamp + timedelta(seconds=1),
            ),
        ),
        formation_event_id,
    )


def test_internal_distribution_preserves_upstream_namespace() -> None:
    assert mem0.__version__ == "2.0.20+viettel.4"
    assert Memory.__module__ == "mem0.memory.main"


def test_v3_add_only_duplicate_preserves_creation_conversation_and_user_scope() -> None:
    fact = "The user prefers revenue figures in billions of VND."
    revised_fact = "The user prefers revenue figures in millions of VND."
    extracted = [fact, fact, revised_fact, fact]
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
        first = memory.add(
            "conversation A creates the preference",
            user_id="user-a",
            metadata={"conversation_id": "conversation-a", "turn_id": "turn-a"},
            infer=True,
        )
        duplicate = memory.add(
            "conversation B repeats the same preference",
            user_id="user-a",
            metadata={"conversation_id": "conversation-b", "turn_id": "turn-b1"},
            infer=True,
        )
        distinct = memory.add(
            "conversation B states a revised preference",
            user_id="user-a",
            metadata={"conversation_id": "conversation-b", "turn_id": "turn-b2"},
            infer=True,
        )
        other_user = memory.add(
            "another user states the first preference",
            user_id="user-b",
            metadata={"conversation_id": "conversation-c", "turn_id": "turn-c"},
            infer=True,
        )

    assert [item["event"] for item in first["results"]] == ["ADD"]
    assert duplicate == {"results": []}
    assert [item["event"] for item in distinct["results"]] == ["ADD"]
    assert [item["event"] for item in other_user["results"]] == ["ADD"]
    assert [row.payload["user_id"] for row in stored] == ["user-a", "user-a", "user-b"]
    assert [row.payload["conversation_id"] for row in stored] == [
        "conversation-a",
        "conversation-b",
        "conversation-c",
    ]
    assert [row.payload["turn_id"] for row in stored] == ["turn-a", "turn-b2", "turn-c"]
    assert [row.payload["data"] for row in stored] == [fact, revised_fact, fact]
    assert first["results"][0]["id"] == stored[0].id
    assert distinct["results"][0]["id"] == stored[1].id
    assert other_user["results"][0]["id"] == stored[2].id
    assert len({row.id for row in stored}) == 3
    assert stored[0].payload["hash"] == hashlib.md5(fact.encode()).hexdigest()
    assert vector_store.insert.call_count == 3
    vector_store.update.assert_not_called()
    vector_store.delete.assert_not_called()

    # Existing-memory context is user-scoped, not conversation-scoped. Conversation B sees A's
    # fact and native V3 drops the exact duplicate without changing A's ID or ownership metadata.
    extraction_prompts = [
        call.kwargs["messages"][1]["content"] for call in llm.generate_response.call_args_list
    ]
    assert fact in extraction_prompts[1]
    assert fact in extraction_prompts[2]
    assert fact not in extraction_prompts[3]

    search_filters = [call.kwargs["filters"] for call in vector_store.search.call_args_list]
    assert search_filters == [
        {"user_id": "user-a"},
        {"user_id": "user-a"},
        {"user_id": "user-a"},
        {"user_id": "user-b"},
    ]


async def test_application_adapter_preserves_creation_conversation_for_duplicate_fact() -> None:
    """Lock the deletion ownership contract at the product adapter boundary.

    Native Mem0 deduplicates an exact fact against all memories for the user.  A later
    conversation that repeats the fact therefore does not acquire another row and does
    not replace the creator conversation in the original row's metadata.
    """

    fact = "The user prefers revenue figures in billions of VND."
    revised_fact = "The user prefers revenue figures in millions of VND."
    extracted = [fact, fact, revised_fact, fact]
    stored: list[StoredVector] = []
    receipts: dict[tuple[str, str], list[dict[str, object]]] = {}

    vector_store = MagicMock()

    def search(*, filters: dict[str, str], **_: object) -> list[StoredVector]:
        return [row for row in stored if row.payload.get("user_id") == filters["user_id"]]

    def get_formation_result(event_id: str, user_id: str):
        return receipts.get((event_id, user_id))

    def insert_with_formation_receipt(
        vectors: list[list[float]],
        ids: list[str],
        payloads: list[dict[str, object]],
        *,
        event_id: str,
        user_id: str,
        result: list[dict[str, object]],
    ) -> tuple[bool, list[dict[str, object]]]:
        del vectors
        key = (event_id, user_id)
        if key in receipts:
            return False, receipts[key]
        stored.extend(
            StoredVector(memory_id, payload)
            for memory_id, payload in zip(ids, payloads, strict=True)
        )
        receipts[key] = result
        return True, result

    vector_store.search.side_effect = search
    vector_store.get_formation_result.side_effect = get_formation_result
    vector_store.insert_with_formation_receipt.side_effect = insert_with_formation_receipt

    embedder = MagicMock()
    embedder.config = MagicMock(embedding_dims=3)
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    embedder.embed_batch.side_effect = lambda texts, _operation: [
        [0.4, 0.5, 0.6] for _ in texts
    ]

    llm = MagicMock()
    llm.generate_response.side_effect = [
        json.dumps({"memory": [{"text": item}]}) for item in extracted
    ]

    history = MagicMock()
    history.get_last_messages.return_value = []

    conversation_a = uuid4()
    conversation_b = uuid4()
    conversation_c = uuid4()
    event_a = uuid4()
    event_b_duplicate = uuid4()
    event_b_revised = uuid4()
    event_c = uuid4()
    sources = (
        _memory_source(
            user_id="user-a",
            conversation_id=conversation_a,
            session_id="session-a",
            turn_id="turn-a",
            boundary_message_id=2,
            formation_event_id=event_a,
        ),
        _memory_source(
            user_id="user-a",
            conversation_id=conversation_b,
            session_id="session-b",
            turn_id="turn-b1",
            boundary_message_id=4,
            formation_event_id=event_b_duplicate,
        ),
        _memory_source(
            user_id="user-a",
            conversation_id=conversation_b,
            session_id="session-b",
            turn_id="turn-b2",
            boundary_message_id=6,
            formation_event_id=event_b_revised,
        ),
        _memory_source(
            user_id="user-b",
            conversation_id=conversation_c,
            session_id="session-c",
            turn_id="turn-c",
            boundary_message_id=8,
            formation_event_id=event_c,
        ),
    )

    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.extract_entities_batch", return_value=[[]]),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=vector_store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        adapter = Mem0Adapter(
            AsyncMemory(MemoryConfig()),
            search_timeout_seconds=2,
            operation_timeout_seconds=5,
        )
        first, duplicate, revised, other_user = [
            await adapter.process_memory(source) for source in sources
        ]

    assert [event.action for event in first.events] == ["ADD"]
    assert duplicate.events == ()
    assert [event.action for event in revised.events] == ["ADD"]
    assert [event.action for event in other_user.events] == ["ADD"]
    assert [row.payload["user_id"] for row in stored] == ["user-a", "user-a", "user-b"]
    assert [row.payload["conversation_id"] for row in stored] == [
        str(conversation_a),
        str(conversation_b),
        str(conversation_c),
    ]
    assert [row.payload["turn_id"] for row in stored] == ["turn-a", "turn-b2", "turn-c"]
    assert [row.payload["data"] for row in stored] == [fact, revised_fact, fact]

    # B's duplicate formation has a durable empty receipt, but no memory row.  Relational
    # deletion by A's ownership therefore removes the shared fact without rebuilding it
    # for B; this is the deliberately bounded product deletion contract.
    assert receipts[(str(event_b_duplicate), "user-a")] == []
    after_deleting_a = [
        row for row in stored if row.payload["conversation_id"] != str(conversation_a)
    ]
    assert not any(
        row.payload["user_id"] == "user-a" and row.payload["data"] == fact
        for row in after_deleting_a
    )
    assert any(row.payload["data"] == revised_fact for row in after_deleting_a)

    vector_store.insert.assert_not_called()
    vector_store.update.assert_not_called()
    vector_store.delete.assert_not_called()
    assert [call.kwargs["filters"] for call in vector_store.search.call_args_list] == [
        {"user_id": "user-a"},
        {"user_id": "user-a"},
        {"user_id": "user-a"},
        {"user_id": "user-b"},
    ]
