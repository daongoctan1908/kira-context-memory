"""Write-free evaluator tests against the vendored native AsyncMemory pipeline."""

import json
from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig
from mem0.observability import (
    current_observation_attribute,
    current_observation_input,
    current_observation_output,
    current_observation_usage,
)

from evaluation.formation import FormationExecutionStatus, WriteFreeFormationEvaluator
from evaluation.models import FormationInput, Message, Outcome

_CONVERSATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_EVENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class ScriptedLlm:
    def __init__(self, responses: Sequence[str | BaseException]) -> None:
        self.responses = iter(responses)
        self.calls = 0

    def generate_response(self, *, messages, **_):
        self.calls += 1
        current_observation_attribute("gen_ai.request.model", "internal-memory-model")
        current_observation_input(messages)
        current_observation_usage({"input_tokens": 21, "completion_tokens": 7, "total_tokens": 28})
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        current_observation_output(response)
        return response


def _inputs(content: str = "Tôi muốn báo cáo gửi tới user@example.com") -> FormationInput:
    return FormationInput(
        user_id="synthetic-user",
        messages=(
            Message(
                message_id="message-1",
                session_id="session-1",
                role="user",
                content=content,
            ),
        ),
    )


def _native_memory(responses: Sequence[str | BaseException]):
    store = MagicMock()
    store.evaluation_write_free = True
    store.get_formation_result.return_value = None
    store.search.return_value = []

    def commit(*_, result, **__):
        return True, result

    store.insert_with_formation_receipt.side_effect = commit
    embedder = MagicMock()
    embedder.config = MagicMock(embedding_dims=3)
    embedder.embed.return_value = [0.1, 0.2, 0.3]
    embedder.embed_batch.side_effect = lambda values, _: [[0.4, 0.5, 0.6] for _ in values]
    llm = ScriptedLlm(responses)
    history = MagicMock()
    history.evaluation_write_free = True
    history.get_last_messages.return_value = []
    entity_store = MagicMock()
    entity_store.evaluation_write_free = True

    patches = (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.extract_entities_batch", return_value=[]),
        patch("mem0.utils.factory.EmbedderFactory.create", return_value=embedder),
        patch("mem0.utils.factory.VectorStoreFactory.create", return_value=store),
        patch("mem0.utils.factory.LlmFactory.create", return_value=llm),
        patch("mem0.memory.main.SQLiteManager", return_value=history),
    )
    for active_patch in patches:
        active_patch.start()
    try:
        memory = AsyncMemory(MemoryConfig())
    finally:
        for active_patch in reversed(patches):
            active_patch.stop()
    memory._entity_store = entity_store
    return memory, llm, store, history


@pytest.mark.asyncio
async def test_write_free_evaluator_returns_valid_facts_with_masked_observation():
    response = json.dumps(
        {
            "memory": [
                {
                    "id": "0",
                    "text": "Người dùng muốn báo cáo qua user@example.com",
                    "attributed_to": "user",
                }
            ]
        }
    )
    memory, llm, store, history = _native_memory([response])

    result = await WriteFreeFormationEvaluator(memory).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs(),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert result.status is FormationExecutionStatus.VALID_FACTS
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert [fact.text for fact in result.facts] == ["Người dùng muốn báo cáo qua user@example.com"]
    assert result.provider_calls == llm.calls == 1
    assert result.model == "internal-memory-model"
    assert result.usage == {"input": 21, "output": 7, "total": 28}
    assert len(result.lifecycle_events) == 1
    assert store.insert_with_formation_receipt.call_count == 1
    assert history.save_messages.call_count == 1
    extraction = next(stage for stage in result.stages if stage.name == "mem0.extract")
    assert extraction.input is not None and extraction.output is not None
    assert "user@example.com" not in (extraction.input.value or "")
    assert "user@example.com" not in (extraction.output.value or "")
    assert "[REDACTED_EMAIL]" in (extraction.output.value or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "status", "outcome", "reason"),
    [
        ('{"memory":[]}', FormationExecutionStatus.VALID_EMPTY, Outcome.REVIEW_REQUIRED, None),
        (
            "not-json-at-all",
            FormationExecutionStatus.MALFORMED,
            Outcome.PROTOCOL_ERROR,
            "formation_malformed_extraction",
        ),
        (
            '{"memory":"invalid-list"}',
            FormationExecutionStatus.PROTOCOL_ERROR,
            Outcome.PROTOCOL_ERROR,
            "formation_protocol_error",
        ),
    ],
)
async def test_write_free_evaluator_distinguishes_empty_malformed_and_protocol_error(
    response, status, outcome, reason
):
    memory, _, _, _ = _native_memory([response])

    result = await WriteFreeFormationEvaluator(memory).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs(),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert result.status is status
    assert result.outcome is outcome
    assert result.reason_codes == ((reason,) if reason else ())


@pytest.mark.asyncio
async def test_write_free_evaluator_classifies_provider_failure_without_raw_error():
    memory, llm, _, _ = _native_memory([RuntimeError("secret provider response")])

    result = await WriteFreeFormationEvaluator(memory).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs(),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert result.status is FormationExecutionStatus.PROVIDER_ERROR
    assert result.outcome is Outcome.DEPENDENCY_ERROR
    assert result.reason_codes == ("formation_provider_error",)
    assert result.provider_calls == llm.calls == 1
    assert "secret provider response" not in result.model_dump_json()


def test_write_free_evaluator_refuses_unmarked_storage():
    client = SimpleNamespace(vector_store=object(), db=object())
    with pytest.raises(ValueError, match="ephemeral vector"):
        WriteFreeFormationEvaluator(client)


@pytest.mark.asyncio
async def test_observer_does_not_change_native_provider_calls_or_extraction_behavior():
    response = json.dumps(
        {"memory": [{"id": "0", "text": "Ưu tiên Hà Nội", "attributed_to": "user"}]}
    )
    without_observer, plain_llm, plain_store, _ = _native_memory([response])
    native = await without_observer.add(
        [{"role": "user", "content": "Ưu tiên Hà Nội"}],
        user_id="synthetic-user",
        metadata={
            "formation_event_id": str(_EVENT_ID),
            "conversation_id": str(_CONVERSATION_ID),
            "turn_id": "message-1",
        },
        infer=True,
    )

    with_observer, observed_llm, observed_store, _ = _native_memory([response])
    observed = await WriteFreeFormationEvaluator(with_observer).evaluate(
        case_id="conv01:formation:event-1",
        inputs=_inputs("Ưu tiên Hà Nội"),
        conversation_id=_CONVERSATION_ID,
        event_id=_EVENT_ID,
    )

    assert plain_llm.calls == observed_llm.calls == 1
    assert [row["memory"] for row in native["results"]] == [fact.text for fact in observed.facts]
    plain_payload = plain_store.insert_with_formation_receipt.call_args.kwargs["payloads"][0]
    observed_payload = observed_store.insert_with_formation_receipt.call_args.kwargs["payloads"][0]
    for key in ("data", "hash", "user_id", "conversation_id", "turn_id", "attributed_to"):
        assert plain_payload[key] == observed_payload[key]
