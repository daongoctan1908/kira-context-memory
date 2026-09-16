from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.domain.errors.conversation import (
    ConversationStoreConnectionError,
    ConversationStoreProtocolError,
)
from app.domain.errors.memory import LongTermMemoryTimeoutError
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import (
    MemoryLifecycleEvent,
    MemoryProcessResult,
)


def reference() -> CompletedTurnReference:
    return CompletedTurnReference("user-1", "session-1", uuid4(), "turn-2", 42)


def messages(*, session_id: str = "session-1", last_turn: str = "turn-2"):
    timestamp = datetime(2026, 9, 7, tzinfo=UTC)
    return (
        ConversationMessage(session_id, "turn-1", ConversationRole.USER, "old question", timestamp),
        ConversationMessage(
            session_id, "turn-1", ConversationRole.ASSISTANT, "old answer", timestamp
        ),
        ConversationMessage(
            session_id, last_turn, ConversationRole.USER, "new question", timestamp
        ),
        ConversationMessage(
            session_id, last_turn, ConversationRole.ASSISTANT, "new answer", timestamp
        ),
    )


class FakeStore:
    def __init__(self, result=(), error=None):
        self.result = tuple(result)
        self.error = error
        self.calls = []

    async def read_through_boundary(self, user_id, conversation_id, boundary_message_id, limit):
        self.calls.append((user_id, conversation_id, boundary_message_id, limit))
        if self.error:
            raise self.error
        return self.result


class FakeMemory:
    def __init__(self, result=None, error=None):
        self.result = result or MemoryProcessResult()
        self.error = error
        self.sources = []

    async def process_memory(self, source):
        self.sources.append(source)
        if self.error:
            raise self.error
        return self.result


class RecordingObservation:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.inputs = []
        self.outputs = []
        self.outcomes = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_input(self, value):
        self.inputs.append(value)

    def set_output(self, value):
        self.outputs.append(value)

    def set_outcome(self, outcome):
        self.outcomes.append(outcome)


class RecordingObserver:
    def __init__(self, *, fail=False):
        self.observations = []
        self.fail = fail

    @contextmanager
    def stage(self, name, *, kind="internal", attributes=None):
        if self.fail:
            raise RuntimeError("observer unavailable")
        observation = RecordingObservation(name)
        observation.attributes.update(attributes or {})
        self.observations.append(observation)
        yield observation


class CallbackFailingObservation:
    @staticmethod
    def _fail(*args):
        raise RuntimeError("observer callback unavailable")

    set_attribute = _fail
    set_input = _fail
    set_output = _fail
    set_outcome = _fail


class CallbackFailingObserver:
    @contextmanager
    def stage(self, name, *, kind="internal", attributes=None):
        yield CallbackFailingObservation()


class ExitFailingManager:
    def __enter__(self):
        return RecordingObservation("failing-exit")

    def __exit__(self, *args):
        raise RuntimeError("observer exit unavailable")


class ExitFailingObserver:
    def stage(self, name, *, kind="internal", attributes=None):
        return ExitFailingManager()


async def test_reads_exact_boundary_and_preserves_provider_lifecycle_result():
    ref = reference()
    formation_event_id = uuid4()
    store = FakeStore(messages())
    expected = MemoryProcessResult(
        (
            MemoryLifecycleEvent("ADD", "memory-1", "fact"),
            MemoryLifecycleEvent("UPDATE", "memory-2", "corrected fact"),
            MemoryLifecycleEvent("DELETE", "memory-3"),
            MemoryLifecycleEvent("NONE"),
        )
    )
    memory = FakeMemory(expected)

    result = await ProcessMemoryUseCase(store, memory, message_limit=4).execute(
        ref,
        formation_event_id,
    )

    assert result is expected
    assert store.calls == [(ref.user_id, ref.conversation_id, ref.boundary_message_id, 4)]
    assert memory.sources[0].reference is ref
    assert memory.sources[0].messages == messages()
    assert memory.sources[0].formation_event_id == formation_event_id


async def test_observes_boundary_and_formation_without_changing_result():
    observer = RecordingObserver()
    memory = FakeMemory(MemoryProcessResult((MemoryLifecycleEvent("ADD", "id", "fact"),)))

    result = await ProcessMemoryUseCase(
        FakeStore(messages()),
        memory,
        observer=observer,
    ).execute(reference(), uuid4())

    assert [item.name for item in observer.observations] == [
        "conversation.read_boundary",
        "mem0.formation",
    ]
    assert observer.observations[0].attributes["kira.memory.message_count"] == 4
    assert observer.observations[1].attributes["kira.memory.lifecycle_event_count"] == 1
    assert observer.observations[1].outcomes == ["success"]
    assert result is memory.result


@pytest.mark.parametrize(
    "observer",
    [
        RecordingObserver(fail=True),
        CallbackFailingObserver(),
        ExitFailingObserver(),
    ],
)
async def test_observer_failure_is_fail_open_for_memory_processing(observer):
    memory = FakeMemory()

    result = await ProcessMemoryUseCase(
        FakeStore(messages()),
        memory,
        observer=observer,
    ).execute(reference(), uuid4())

    assert result is memory.result


@pytest.mark.parametrize(
    "snapshot",
    [
        (),
        messages(session_id="other-session"),
        messages(last_turn="other-turn"),
        messages()[:-1],
        (messages()[0], messages()[2]),
        (messages()[1], messages()[0]),
    ],
)
async def test_rejects_empty_cross_session_or_non_boundary_snapshot(snapshot):
    memory = FakeMemory()

    with pytest.raises(ConversationStoreProtocolError):
        await ProcessMemoryUseCase(FakeStore(snapshot), memory).execute(reference(), uuid4())

    assert memory.sources == []


async def test_store_and_memory_typed_failures_propagate_to_runtime_boundary():
    store_error = ConversationStoreConnectionError()
    with pytest.raises(ConversationStoreConnectionError):
        await ProcessMemoryUseCase(FakeStore(error=store_error), FakeMemory()).execute(
            reference(),
            uuid4(),
        )

    memory_error = LongTermMemoryTimeoutError()
    with pytest.raises(LongTermMemoryTimeoutError):
        await ProcessMemoryUseCase(FakeStore(messages()), FakeMemory(error=memory_error)).execute(
            reference(),
            uuid4(),
        )


@pytest.mark.parametrize(
    "observer",
    [
        None,
        RecordingObserver(),
        RecordingObserver(fail=True),
        CallbackFailingObserver(),
        ExitFailingObserver(),
    ],
)
async def test_observer_mode_preserves_retryable_formation_error(observer):
    memory_error = LongTermMemoryTimeoutError()

    with pytest.raises(LongTermMemoryTimeoutError) as captured:
        await ProcessMemoryUseCase(
            FakeStore(messages()),
            FakeMemory(error=memory_error),
            observer=observer,
        ).execute(reference(), uuid4())

    assert captured.value is memory_error


async def test_rejects_non_uuid_formation_event_before_reading_store():
    store = FakeStore(messages())

    with pytest.raises(TypeError, match="formation_event_id"):
        await ProcessMemoryUseCase(store, FakeMemory()).execute(
            reference(),
            "not-a-uuid",  # type: ignore[arg-type]
        )

    assert store.calls == []


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_formation_limit_must_preserve_complete_turn_boundaries(limit):
    with pytest.raises(ValueError, match="turn boundary"):
        ProcessMemoryUseCase(FakeStore(), FakeMemory(), message_limit=limit)
