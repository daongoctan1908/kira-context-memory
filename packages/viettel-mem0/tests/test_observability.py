import asyncio
import hashlib
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.configs.llms.base import BaseLlmConfig
from mem0.embeddings.openai import OpenAIEmbedding
from mem0.exceptions import LLMError
from mem0.llms.vllm import VllmLLM
from mem0.memory.main import AsyncMemory
from mem0.observability import (
    bind_observer,
    current_observation_attribute,
    current_observation_response_usage,
    current_observation_usage,
    observe,
)


class RecordingObservation:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.inputs = []
        self.outputs = []
        self.usages = []
        self.outcomes = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_input(self, value):
        self.inputs.append(value)

    def set_output(self, value):
        self.outputs.append(value)

    def set_usage(self, usage):
        self.usages.append(dict(usage))

    def set_outcome(self, outcome):
        self.outcomes.append(outcome)


class RecordingObserver:
    def __init__(self):
        self.observations = []

    @contextmanager
    def observe(self, name, *, kind="internal", attributes=None):
        observation = RecordingObservation(name)
        observation.attributes.update(attributes or {})
        observation.attributes["kind"] = kind
        self.observations.append(observation)
        yield observation


class FailingObserver:
    def observe(self, *args, **kwargs):
        raise RuntimeError("observer unavailable")


class FailingObservation:
    def __getattr__(self, name):
        raise RuntimeError("observer callback unavailable")


class CallbackFailingObserver:
    @contextmanager
    def observe(self, *args, **kwargs):
        yield FailingObservation()


class ExitFailingManager:
    def __enter__(self):
        return RecordingObservation("mem0.extract")

    def __exit__(self, *args):
        raise RuntimeError("observer exit unavailable")


class ExitFailingObserver:
    def observe(self, *args, **kwargs):
        return ExitFailingManager()


def test_observer_failure_and_exit_failure_do_not_change_business_result():
    with bind_observer(FailingObserver()):
        with observe("mem0.extract") as observation:
            observation.set_input("private")
            result = "business-result"

    assert result == "business-result"

    with bind_observer(CallbackFailingObserver()):
        with observe("mem0.extract") as observation:
            observation.set_input("private")
            observation.set_usage({"input": 1})
            observation.set_outcome("success")
            result = "same-business-result"

    assert result == "same-business-result"

    with bind_observer(ExitFailingObserver()):
        with observe("mem0.extract"):
            result = "exit-failure-business-result"

    assert result == "exit-failure-business-result"


def test_observer_exit_failure_preserves_business_exception():
    business_error = ValueError("business failure")

    with pytest.raises(ValueError) as captured:
        with bind_observer(ExitFailingObserver()):
            with observe("mem0.extract"):
                raise business_error

    assert captured.value is business_error


@pytest.mark.asyncio
async def test_invocation_context_is_isolated_and_propagates_to_threads():
    async def run(label):
        observer = RecordingObserver()
        with bind_observer(observer):
            with observe("mem0.extract"):
                await asyncio.to_thread(current_observation_attribute, "label", label)
        return observer

    left, right = await asyncio.gather(run("left"), run("right"))

    assert left.observations[0].attributes["label"] == "left"
    assert right.observations[0].attributes["label"] == "right"


def test_usage_parser_ignores_absent_and_malformed_counts():
    observer = RecordingObserver()
    with bind_observer(observer):
        with observe("mem0.extract"):
            current_observation_usage(None)
            current_observation_usage({"prompt_tokens": "bad", "completion_tokens": -1})
            current_observation_usage(SimpleNamespace(prompt_tokens=7, completion_tokens=2, total_tokens=9))

    assert observer.observations[0].usages == [{"input": 7, "output": 2, "total": 9}]


def test_usage_property_failure_is_fail_open():
    class BrokenResponse:
        @property
        def usage(self):
            raise RuntimeError("private provider detail")

    observer = RecordingObserver()
    with bind_observer(observer):
        with observe("mem0.extract"):
            current_observation_response_usage(BrokenResponse())
            result = "provider-result"

    assert result == "provider-result"
    assert observer.observations[0].usages == []


def test_vllm_captures_usage_before_returning_plain_text():
    observer = RecordingObserver()
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
    )
    with patch("mem0.llms.vllm.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value = response
        llm = VllmLLM(BaseLlmConfig(model="model"))
        with bind_observer(observer):
            with observe("mem0.extract"):
                result = llm.generate_response([{"role": "user", "content": "question"}])

    observation = observer.observations[0]
    assert result == "answer"
    assert observation.attributes["gen_ai.request.model"] == "model"
    assert observation.usages == [{"input": 5, "output": 2, "total": 7}]
    assert observation.outputs == ["answer"]


def test_embedding_captures_usage_without_recording_vector():
    observer = RecordingObserver()
    response = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.1, 0.2])],
        usage=SimpleNamespace(prompt_tokens=3, total_tokens=3),
    )
    with patch("mem0.embeddings.openai.OpenAI") as openai:
        openai.return_value.embeddings.create.return_value = response
        embedder = OpenAIEmbedding(BaseEmbedderConfig(model="embed-model"))
        with bind_observer(observer):
            with observe("mem0.memory.embed"):
                result = embedder.embed("private input")

    observation = observer.observations[0]
    assert result == [0.1, 0.2]
    assert observation.attributes["gen_ai.request.model"] == "embed-model"
    assert observation.usages == [{"input": 3, "total": 3}]
    assert observation.inputs == []
    assert observation.outputs == []


def _memory(response, *, existing=None, receipt=None):
    vector_store = Mock()
    vector_store.get_formation_result.return_value = receipt
    vector_store.search.return_value = list(existing or [])
    vector_store.insert_with_formation_receipt.side_effect = lambda *args, **kwargs: (True, kwargs.get("result", []))
    embedding = Mock()
    embedding.embed.return_value = [0.1, 0.2]
    embedding.embed_batch.side_effect = lambda texts, action: [[0.2, 0.1] for _ in texts]
    database = Mock()
    database.get_last_messages.return_value = []
    memory = SimpleNamespace(
        vector_store=vector_store,
        embedding_model=embedding,
        llm=Mock(generate_response=Mock(return_value=response)),
        db=database,
        custom_instructions=None,
        api_version="v1.1",
    )
    return memory


async def _form(memory, observer=None):
    with bind_observer(observer):
        return await AsyncMemory._add_to_vector_store(
            memory,
            messages=[{"role": "user", "content": "User likes tea"}],
            metadata={
                "formation_event_id": "11111111-1111-1111-1111-111111111111",
                "conversation_id": "33333333-3333-3333-3333-333333333333",
                "created_at": "2026-09-15T00:00:00+00:00",
            },
            effective_filters={"user_id": "user-1"},
            infer=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,parse_outcome",
    [('{"memory": []}', "empty_valid"), ("not-json", "malformed")],
)
async def test_empty_and_malformed_extraction_have_distinct_parse_outcomes(response, parse_outcome):
    observer = RecordingObserver()
    memory = _memory(response)

    result = await _form(memory, observer)

    outcomes = {item.name: item.outcomes[-1] for item in observer.observations}
    assert result == []
    assert outcomes["mem0.extract.parse"] == parse_outcome
    assert outcomes["mem0.persist"] == "empty"


@pytest.mark.asyncio
async def test_provider_failure_is_attributed_to_extraction_stage():
    observer = RecordingObserver()
    memory = _memory('{"memory": []}')
    memory.llm.generate_response.side_effect = RuntimeError("provider failed")

    with pytest.raises(Exception):
        await _form(memory, observer)

    extract = next(item for item in observer.observations if item.name == "mem0.extract")
    assert extract.outcomes[-1] == "error"
    assert extract.attributes["error.type"] == "LLMError"


@pytest.mark.asyncio
async def test_dedup_to_empty_and_receipt_replay_are_distinguishable():
    text = "User likes tea"
    existing = [
        SimpleNamespace(
            id="memory-1",
            payload={"data": text, "hash": hashlib.md5(text.encode()).hexdigest()},
        )
    ]
    dedup_observer = RecordingObserver()
    await _form(_memory('{"memory": [{"text": "User likes tea"}]}', existing=existing), dedup_observer)
    dedup = next(item for item in dedup_observer.observations if item.name == "mem0.deduplicate")
    persist = next(item for item in dedup_observer.observations if item.name == "mem0.persist")
    assert dedup.outcomes[-1] == "empty"
    assert persist.outcomes[-1] == "deduplicated_empty"

    committed = [{"id": "memory-1", "memory": text, "event": "ADD"}]
    replay_observer = RecordingObserver()
    result = await _form(_memory("must not run", receipt=committed), replay_observer)
    assert result == committed
    assert [(item.name, item.outcomes[-1]) for item in replay_observer.observations] == [("mem0.receipt", "replay")]


@pytest.mark.asyncio
async def test_observer_on_and_off_leave_provider_call_result_and_receipt_unchanged():
    response = '{"memory": [{"text": "User likes tea"}]}'
    without_observer = _memory(response)
    with_observer = _memory(response)
    with_failing_observer = _memory(response)
    fixed_id = UUID("22222222-2222-2222-2222-222222222222")

    with (
        patch("mem0.memory.main.uuid.uuid4", return_value=fixed_id),
        patch("mem0.memory.main.extract_entities_batch", return_value=[[]]),
        patch("mem0.memory.main.capture_event"),
    ):
        plain_result = await _form(without_observer)
        observed_result = await _form(with_observer, RecordingObserver())
        fail_open_result = await _form(with_failing_observer, FailingObserver())

    assert observed_result == plain_result
    assert fail_open_result == plain_result
    for candidate in (with_observer, with_failing_observer):
        assert candidate.vector_store.get_formation_result.call_args_list == (
            without_observer.vector_store.get_formation_result.call_args_list
        )
        assert candidate.embedding_model.embed.call_args_list == (without_observer.embedding_model.embed.call_args_list)
        assert candidate.vector_store.search.call_args_list == (without_observer.vector_store.search.call_args_list)
        assert candidate.llm.generate_response.call_args_list == (without_observer.llm.generate_response.call_args_list)
        assert candidate.embedding_model.embed_batch.call_args_list == (
            without_observer.embedding_model.embed_batch.call_args_list
        )
        assert candidate.vector_store.insert_with_formation_receipt.call_args_list == (
            without_observer.vector_store.insert_with_formation_receipt.call_args_list
        )
        assert candidate.db.save_messages.call_args_list == (without_observer.db.save_messages.call_args_list)
        assert candidate.db.batch_add_history.call_args_list == (without_observer.db.batch_add_history.call_args_list)


@pytest.mark.asyncio
async def test_observer_on_off_and_failure_preserve_provider_error_semantics_and_writes():
    memories = [_memory('{"memory": []}') for _ in range(3)]
    observers = (None, RecordingObserver(), FailingObserver())
    errors = []
    for memory, observer in zip(memories, observers):
        memory.llm.generate_response.side_effect = RuntimeError("private provider detail")
        with pytest.raises(LLMError) as captured:
            await _form(memory, observer)
        errors.append(captured.value)

    assert [str(error) for error in errors] == [str(errors[0])] * 3
    assert [type(error.__cause__) for error in errors] == [RuntimeError] * 3
    assert [str(error.__cause__) for error in errors] == ["private provider detail"] * 3
    for candidate in memories[1:]:
        assert candidate.embedding_model.embed.call_args_list == (memories[0].embedding_model.embed.call_args_list)
        assert candidate.vector_store.search.call_args_list == (memories[0].vector_store.search.call_args_list)
        assert candidate.llm.generate_response.call_args_list == (memories[0].llm.generate_response.call_args_list)
    for memory in memories:
        memory.embedding_model.embed_batch.assert_not_called()
        memory.vector_store.insert_with_formation_receipt.assert_not_called()
        memory.db.save_messages.assert_not_called()
        memory.db.batch_add_history.assert_not_called()
