"""Per-call source context and event-preserving formation contracts."""

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from mem0 import AsyncMemory, Memory
from mem0.configs.base import MemoryConfig
from mem0.configs.prompts import generate_additive_extraction_prompt


@dataclass
class StoredVector:
    id: str
    payload: dict


def providers():
    store = MagicMock()
    store.get_formation_result.return_value = None
    store.search.return_value = []
    store.insert_with_formation_receipt.side_effect = lambda *args, **kwargs: (
        True,
        kwargs["result"],
    )
    embedder = MagicMock()
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    embedder.embed_batch.side_effect = lambda texts, _: [[0.4, 0.5, 0.6] for _ in texts]
    llm = MagicMock()
    llm.generate_response.return_value = '{"memory": []}'
    history = MagicMock()
    history.get_last_messages.return_value = [{"role": "user", "content": "native history"}]
    return store, embedder, llm, history


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "context", [None, [], [{"role": "assistant", "content": "source context"}]]
)
async def test_per_call_context_overrides_native_history_only_when_supplied(asynchronous, context):
    store, embedder, llm, history = providers()
    pair = [{"role": "user", "content": "New assertion"}]
    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        memory = (AsyncMemory if asynchronous else Memory)(MemoryConfig())
        kwargs = {"user_id": "owner", "last_k_messages": context}
        if asynchronous:
            await memory.add(pair, **kwargs)
        else:
            memory.add(pair, **kwargs)
    prompt = llm.generate_response.call_args.kwargs["messages"][1]["content"]
    if context is None:
        history.get_last_messages.assert_called_once()
        assert "## Last k Messages\nuser: native history" in prompt
    else:
        history.get_last_messages.assert_not_called()
        assert "native history" not in prompt
        if context:
            assert "## Last k Messages\nassistant: source context" in prompt
        else:
            last_section = prompt.split("## Last k Messages\n")[1].split("## Recently Extracted")[0]
            assert not last_section.strip()
    assert "## New Messages\nuser: New assertion" in prompt
    assert history.save_messages.call_args.args[0] == pair


def test_preceding_context_preserves_complete_long_proposal():
    proposal = "A" * 310 + " Formula = A / B; exclude TEST_003."
    prompt = generate_additive_extraction_prompt(
        last_k_messages=[{"role": "assistant", "content": proposal}],
        new_messages=[{"role": "user", "content": "Adopt that formula."}],
    )
    assert proposal in prompt
    assert "exclude TEST_003." in prompt


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_source_timestamp_anchors_observation_and_receipt_replay_bypasses_context(
    asynchronous,
):
    store, embedder, llm, history = providers()
    event_id, conversation_id = str(uuid4()), str(uuid4())
    kwargs = {
        "user_id": "owner",
        "run_id": conversation_id,
        "last_k_messages": [],
        "metadata": {
            "formation_event_id": event_id,
            "conversation_id": conversation_id,
            "source_timestamp": "2026-09-13T00:05:00+07:00",
        },
    }
    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        memory = (AsyncMemory if asynchronous else Memory)(MemoryConfig())
        if asynchronous:
            await memory.add("Only today, use this convention.", **kwargs)
        else:
            memory.add("Only today, use this convention.", **kwargs)
        store.get_formation_result.return_value = []
        if asynchronous:
            await memory.add("Only today, use this convention.", **kwargs)
        else:
            memory.add("Only today, use this convention.", **kwargs)
    prompt = llm.generate_response.call_args.kwargs["messages"][1]["content"]
    assert "## Observation Date\n2026-09-13T00:05:00+07:00" in prompt
    llm.generate_response.assert_called_once()
    embedder.embed.assert_called_once()
    history.get_last_messages.assert_not_called()
    history.save_messages.assert_called_once()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "scope,event_scoped,expected",
    [("GLOBAL", True, 3), ("CONVERSATION", True, 2), ("GLOBAL", False, 2)],
)
async def test_reinstatement_preserves_global_event_without_changing_native_dedup(
    asynchronous, scope, event_scoped, expected
):
    store, embedder, llm, history = providers()
    stored = []
    receipts = {}
    store.search.side_effect = lambda **_: list(stored)
    store.get_formation_result.side_effect = lambda event, *_: receipts.get(event)

    def insert(*, ids, payloads, **_):
        stored.extend(StoredVector(i, p) for i, p in zip(ids, payloads, strict=True))

    def receipt(vectors, ids, payloads, *, event_id, result, **_):
        if event_id in receipts:
            return False, receipts[event_id]
        insert(ids=ids, payloads=payloads)
        receipts[event_id] = result
        return True, result

    store.insert.side_effect = insert
    store.insert_with_formation_receipt.side_effect = receipt
    facts = ["KPI_X < 98%", "KPI_X < 99%", "KPI_X < 98%"]
    # Controlled outputs prove persistence semantics, not live extractor accuracy.
    # Repeating within a batch must still produce at most one assertion.
    llm.generate_response.side_effect = [
        json.dumps({"memory": [{"text": fact, "scope": scope}] * 2}) for fact in facts
    ]
    conversation_id = str(uuid4())
    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.extract_entities_batch", return_value=[[]]),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        memory = (AsyncMemory if asynchronous else Memory)(MemoryConfig())
        for fact in facts:
            metadata = {"conversation_id": conversation_id}
            if event_scoped:
                metadata["formation_event_id"] = str(uuid4())
            kwargs = {"user_id": "owner", "run_id": conversation_id, "metadata": metadata}
            if asynchronous:
                last = await memory.add(fact, **kwargs)
            else:
                last = memory.add(fact, **kwargs)
        if event_scoped:
            if asynchronous:
                replay = await memory.add(facts[-1], **kwargs)
            else:
                replay = memory.add(facts[-1], **kwargs)
            assert replay == last
    assert len(stored) == expected
    assert llm.generate_response.call_count == 3
    assert [row.payload["data"] for row in stored] == facts[:expected]


async def test_concurrent_per_call_context_stays_with_its_source_event():
    store, embedder, llm, history = providers()
    prompts = []
    llm.generate_response.side_effect = lambda **kwargs: (
        prompts.append(kwargs["messages"][1]["content"]) or '{"memory": []}'
    )
    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    ):
        memory = AsyncMemory(MemoryConfig(history_enabled=False))
        await asyncio.gather(
            memory.add(
                "adopt source A",
                user_id="owner",
                run_id=str(uuid4()),
                last_k_messages=[{"role": "assistant", "content": "context A"}],
            ),
            memory.add(
                "adopt source B",
                user_id="owner",
                run_id=str(uuid4()),
                last_k_messages=[{"role": "assistant", "content": "context B"}],
            ),
        )
    assert len(prompts) == 2
    for prompt in prompts:
        if "adopt source A" in prompt:
            assert "context A" in prompt
            assert "context B" not in prompt
        else:
            assert "adopt source B" in prompt
            assert "context B" in prompt
            assert "context A" not in prompt
