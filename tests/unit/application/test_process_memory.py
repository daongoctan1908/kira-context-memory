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


async def test_reads_exact_boundary_and_preserves_provider_lifecycle_result():
    ref = reference()
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

    result = await ProcessMemoryUseCase(store, memory, message_limit=4).execute(ref)

    assert result is expected
    assert store.calls == [(ref.user_id, ref.conversation_id, ref.boundary_message_id, 4)]
    assert memory.sources[0].reference is ref
    assert memory.sources[0].messages == messages()


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
        await ProcessMemoryUseCase(FakeStore(snapshot), memory).execute(reference())

    assert memory.sources == []


async def test_store_and_memory_typed_failures_propagate_to_runtime_boundary():
    store_error = ConversationStoreConnectionError()
    with pytest.raises(ConversationStoreConnectionError):
        await ProcessMemoryUseCase(FakeStore(error=store_error), FakeMemory()).execute(reference())

    memory_error = LongTermMemoryTimeoutError()
    with pytest.raises(LongTermMemoryTimeoutError):
        await ProcessMemoryUseCase(FakeStore(messages()), FakeMemory(error=memory_error)).execute(
            reference()
        )


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_formation_limit_must_preserve_complete_turn_boundaries(limit):
    with pytest.raises(ValueError, match="turn boundary"):
        ProcessMemoryUseCase(FakeStore(), FakeMemory(), message_limit=limit)
