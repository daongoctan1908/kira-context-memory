"""Scope enforcement in the vendored extraction pipeline (T1.3).

The extraction LLM classifies each candidate with a scope. The vendored pipeline
is the single decision point: valid scopes persist with `memory_scope` in the
payload, missing scopes fall back to CONVERSATION with a counter, and invalid
enum values drop the candidate before any hash, embed, or write.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig

from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import MemorySource
from app.infrastructure.memory.mem0_adapter import Mem0Adapter
from app.infrastructure.observability.memory_observer import MemoryObserver


def _source() -> MemorySource:
    return MemorySource(
        CompletedTurnReference("user-a", "session-a", uuid4(), "turn-a", 2),
        (
            ConversationMessage(
                "session-a",
                "turn-a",
                ConversationRole.USER,
                "Tuần này ưu tiên Hà Nội.",
                datetime(2026, 9, 24, 2, tzinfo=UTC),
            ),
            ConversationMessage(
                "session-a",
                "turn-a",
                ConversationRole.ASSISTANT,
                "Đã rõ.",
                datetime(2026, 9, 24, 2, 1, tzinfo=UTC),
            ),
        ),
        uuid4(),
    )


def _setup_store():
    store = MagicMock()
    store.get_formation_result.return_value = None
    store.search.return_value = []

    def insert_with_formation_receipt(
        vectors, ids, payloads, *, event_id, user_id, conversation_id, result
    ):
        store.last_receipt_payloads = list(payloads)
        store.last_receipt_vectors = list(vectors)
        return True, list(result)

    store.insert_with_formation_receipt.side_effect = insert_with_formation_receipt
    return store


async def _process(store, embedder, llm, history, source=None, observer=None):
    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.extract_entities_batch", return_value=[[]]),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        memory = AsyncMemory(MemoryConfig())
        adapter = Mem0Adapter(
            memory,
            search_timeout_seconds=2,
            operation_timeout_seconds=5,
            observer=observer,
        )
        return await adapter.process_memory(source or _source())


def _embedder_and_llm(llm_response: str):
    embedder = MagicMock()
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    embedder.embed_batch.side_effect = lambda texts, _operation: [[0.4, 0.5, 0.6] for _ in texts]
    llm = MagicMock()
    llm.generate_response.return_value = llm_response
    history = MagicMock()
    history.get_last_messages.return_value = []
    return embedder, llm, history


async def test_valid_scopes_persist_with_memory_scope_in_payload():
    store = _setup_store()
    response = json.dumps(
        {
            "memory": [
                {"text": "User prefers short Vietnamese reports.", "scope": "GLOBAL"},
                {"text": "This week focuses on Hanoi.", "scope": "CONVERSATION"},
            ]
        }
    )
    embedder, llm, history = _embedder_and_llm(response)

    result = await _process(store, embedder, llm, history)

    assert [event.action for event in result.events] == ["ADD", "ADD"]
    assert [payload["memory_scope"] for payload in store.last_receipt_payloads] == [
        "GLOBAL",
        "CONVERSATION",
    ]


async def test_missing_scope_falls_back_to_conversation():
    store = _setup_store()
    response = json.dumps({"memory": [{"text": "Fact without any scope field."}]})
    embedder, llm, history = _embedder_and_llm(response)

    result = await _process(store, embedder, llm, history)

    assert [event.action for event in result.events] == ["ADD"]
    assert store.last_receipt_payloads[0]["memory_scope"] == "CONVERSATION"


async def test_invalid_scope_drops_candidate_before_embed_and_write():
    store = _setup_store()
    response = json.dumps(
        {
            "memory": [
                {"text": "Valid fact with scope.", "scope": "GLOBAL"},
                {"text": "Fact with broken scope.", "scope": "galaxy"},
            ]
        }
    )
    embedder, llm, history = _embedder_and_llm(response)

    result = await _process(store, embedder, llm, history)

    assert [event.action for event in result.events] == ["ADD"]
    assert len(store.last_receipt_payloads) == 1
    assert store.last_receipt_payloads[0]["data"] == "Valid fact with scope."
    assert store.last_receipt_payloads[0]["memory_scope"] == "GLOBAL"
    # The invalid candidate must never reach the embed batch.
    embedded_texts = embedder.embed_batch.call_args.args[0]
    assert "Fact with broken scope." not in embedded_texts
    assert "Valid fact with scope." in embedded_texts


async def test_all_invalid_scopes_persist_nothing_but_commit_receipt():
    store = _setup_store()
    response = json.dumps(
        {
            "memory": [
                {"text": "Broken scope fact.", "scope": "CONVERS"},
                {"text": "Numeric scope fact.", "scope": 7},
            ]
        }
    )
    embedder, llm, history = _embedder_and_llm(response)

    result = await _process(store, embedder, llm, history)

    assert result.events == ()
    assert store.last_receipt_vectors == []
    embedder.embed_batch.assert_not_called()


async def test_receipt_replay_creates_no_new_write_and_keeps_persisted_scope():
    """Replay is idempotent: zero new writes and the persisted payload keeps its scope.

    The add() response only exposes lifecycle rows {id, memory, event}, so scope
    assertions read the store's persisted payloads, not the replay response.
    """
    store = _setup_store()
    persisted: list[dict] = []

    def get_formation_result(event_id, user_id, conversation_id):
        return store.receipts.get((event_id, user_id))

    def insert_with_formation_receipt(
        vectors, ids, payloads, *, event_id, user_id, conversation_id, result
    ):
        if (event_id, user_id) in store.receipts:
            return False, store.receipts[(event_id, user_id)]
        store.receipts[(event_id, user_id)] = list(result)
        store.last_receipt_payloads = list(payloads)
        store.last_receipt_vectors = list(vectors)
        persisted.extend(dict(payload) for payload in payloads)
        return True, list(result)

    store.receipts = {}
    store.get_formation_result.side_effect = get_formation_result
    store.insert_with_formation_receipt.side_effect = insert_with_formation_receipt
    response = json.dumps(
        {
            "memory": [
                {"text": "User prefers short Vietnamese reports.", "scope": "GLOBAL"},
                {"text": "This week focuses on Hanoi.", "scope": "CONVERSATION"},
            ]
        }
    )
    embedder, llm, history = _embedder_and_llm(response)
    source = _source()

    first = await _process(store, embedder, llm, history, source=source)
    payloads_after_first = [dict(payload) for payload in persisted]
    embed_calls_after_first = embedder.embed_batch.call_count
    llm_calls_after_first = llm.generate_response.call_count

    replay = await _process(store, embedder, llm, history, source=source)

    assert [event.action for event in first.events] == ["ADD", "ADD"]
    # Replay returns the committed receipt result without new writes or provider calls.
    assert [event.action for event in replay.events] == ["ADD", "ADD"]
    assert len(persisted) == 2
    assert embedder.embed_batch.call_count == embed_calls_after_first
    assert llm.generate_response.call_count == llm_calls_after_first
    # Persisted payload scope is unchanged after replay (read from store, not response).
    assert [payload["memory_scope"] for payload in persisted] == ["GLOBAL", "CONVERSATION"]
    assert payloads_after_first == persisted


class ScopeDistributionRecorder:
    def __init__(self):
        self.calls = []
        self.stage_calls = []

    def formation_scope_observed(self, *, conversation, global_count, fallback, invalid):
        self.calls.append(
            {
                "conversation": conversation,
                "global_count": global_count,
                "fallback": fallback,
                "invalid": invalid,
            }
        )

    def stage_observed(self, stage, outcome, seconds):
        self.stage_calls.append((stage, outcome, seconds))


async def test_scope_distribution_flows_through_observer_bridge():
    recorder = ScopeDistributionRecorder()
    observer = MemoryObserver(metric_observer=recorder)
    store = _setup_store()
    response = json.dumps(
        {
            "memory": [
                {"text": "User prefers short Vietnamese reports.", "scope": "GLOBAL"},
                {"text": "This week focuses on Hanoi.", "scope": "CONVERSATION"},
                {"text": "Fact without any scope field."},
                {"text": "Fact with broken scope.", "scope": "galaxy"},
            ]
        }
    )
    embedder, llm, history = _embedder_and_llm(response)

    await _process(store, embedder, llm, history, observer=observer)

    # 2 valid (GLOBAL + CONVERSATION), 1 fallback, 1 invalid dropped before embed.
    assert recorder.calls == [{"conversation": 1, "global_count": 1, "fallback": 1, "invalid": 1}]
    assert ("mem0.extract.scope", "enforced") in [
        (stage, outcome) for stage, outcome, _ in recorder.stage_calls
    ]
    # The invalid candidate still never reaches the embed batch.
    embedded_texts = embedder.embed_batch.call_args.args[0]
    assert "Fact with broken scope." not in embedded_texts


async def test_all_valid_scope_formation_still_reports_zero_fallback_and_invalid():
    recorder = ScopeDistributionRecorder()
    observer = MemoryObserver(metric_observer=recorder)
    store = _setup_store()
    response = json.dumps(
        {
            "memory": [
                {"text": "User prefers short Vietnamese reports.", "scope": "GLOBAL"},
                {"text": "This week focuses on Hanoi.", "scope": "CONVERSATION"},
            ]
        }
    )
    embedder, llm, history = _embedder_and_llm(response)

    await _process(store, embedder, llm, history, observer=observer)

    # Valid-only batches still report so the scope distribution has a full denominator.
    assert recorder.calls == [{"conversation": 1, "global_count": 1, "fallback": 0, "invalid": 0}]
