import pytest

from app.application.services.memory_retriever import ScopedMemoryRetriever
from app.domain.errors.memory import (
    LongTermMemoryConnectionError,
    LongTermMemoryProtocolError,
)
from app.domain.models.memory import LongTermMemory


def memory(memory_id, content, score):
    return LongTermMemory(memory_id, content, score, {})


def branch_implementation(calls, *, conversation=(), global_memories=(), error=None):
    class BranchMemory:
        async def search_scoped(self, user_id, query, *, conversation_id, scope, top_k, threshold):
            calls.append(scope)
            if error is not None:
                raise error
            return conversation if scope == "conversation" else global_memories

        async def search(self, user_id, query, *, top_k, threshold):
            raise AssertionError("legacy search must not be used by the retriever")

    return BranchMemory()


CONVERSATION_ID = "0f7d2c1e-0000-4000-8000-000000000001"


async def test_merge_interleaves_branches_by_raw_score():
    calls = []
    retriever = ScopedMemoryRetriever(
        branch_implementation(
            calls,
            conversation=(memory("c1", "local", 0.5), memory("c2", "local 2", 0.7)),
            global_memories=(memory("g1", "global", 0.9), memory("g2", "global 2", 0.6)),
        )
    )

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert [item.memory_id for item in result] == ["g1", "c2", "g2", "c1"]
    assert calls == ["conversation", "global"]


async def test_merge_dedups_by_memory_id_and_content_preferring_conversation_local():
    calls = []
    retriever = ScopedMemoryRetriever(
        branch_implementation(
            calls,
            conversation=(
                memory("shared", "same text", 0.5),
                memory("c2", "duPlicated   text", 0.4),
            ),
            global_memories=(
                memory("shared", "global version", 0.9),
                memory("g2", "duplicated text", 0.8),
            ),
        )
    )

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    # g2 ("duplicated text") is a content duplicate of c2 ("duPlicated   text");
    # the conversation-local record wins the normalized-content tie.
    assert [item.memory_id for item in result] == ["shared", "c2"]
    assert result[0].content == "same text"


async def test_merge_caps_result_at_top_k():
    calls = []
    retriever = ScopedMemoryRetriever(
        branch_implementation(
            calls,
            conversation=tuple(memory(f"c{i}", f"local {i}", 0.5) for i in range(6)),
            global_memories=tuple(memory(f"g{i}", f"global {i}", 0.5) for i in range(6)),
        )
    )

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=7, threshold=0.1
    )

    assert len(result) == 7


async def test_merge_tie_breaks_by_memory_id_ascending():
    calls = []
    retriever = ScopedMemoryRetriever(
        branch_implementation(
            calls,
            conversation=(memory("b-memory", "b", 0.5),),
            global_memories=(memory("a-memory", "a", 0.5),),
        )
    )

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert [item.memory_id for item in result] == ["a-memory", "b-memory"]


async def test_fail_open_when_only_conversation_branch_fails():
    calls = []
    retriever = ScopedMemoryRetriever(
        branch_implementation(
            calls,
            global_memories=(memory("g1", "global", 0.9),),
        )
    )
    original = retriever._memory.search_scoped

    async def patched(*args, **kwargs):
        if kwargs.get("scope") == "conversation":
            raise LongTermMemoryConnectionError
        return await original(*args, **kwargs)

    retriever._memory.search_scoped = patched

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert [item.memory_id for item in result] == ["g1"]


async def test_fail_open_when_only_global_branch_fails():
    calls = []
    retriever = ScopedMemoryRetriever(
        branch_implementation(
            calls,
            conversation=(memory("c1", "local", 0.9),),
        )
    )

    async def fail_global(*args, **kwargs):
        raise LongTermMemoryConnectionError

    original = retriever._memory.search_scoped

    async def patched(*args, **kwargs):
        if kwargs.get("scope") == "global":
            raise LongTermMemoryConnectionError
        return await original(*args, **kwargs)

    retriever._memory.search_scoped = patched

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert [item.memory_id for item in result] == ["c1"]


async def test_raises_when_both_branches_fail():
    retriever = ScopedMemoryRetriever(
        branch_implementation([], error=LongTermMemoryConnectionError())
    )

    with pytest.raises(LongTermMemoryConnectionError):
        await retriever.search(
            "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
        )


async def test_protocol_violation_is_treated_as_branch_failure():
    class MalformedMemory:
        async def search_scoped(self, user_id, query, *, conversation_id, scope, top_k, threshold):
            return ("not-a-memory",) if scope == "conversation" else ()

    retriever = ScopedMemoryRetriever(MalformedMemory())

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert result == ()


async def test_protocol_violation_on_both_branches_raises():
    class MalformedMemory:
        async def search_scoped(self, user_id, query, *, conversation_id, scope, top_k, threshold):
            return ("not-a-memory",)

    retriever = ScopedMemoryRetriever(MalformedMemory())

    with pytest.raises(LongTermMemoryProtocolError):
        await retriever.search(
            "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
        )


async def test_protocol_violation_fails_open_to_healthy_branch():
    class HalfMalformedMemory:
        async def search_scoped(self, user_id, query, *, conversation_id, scope, top_k, threshold):
            if scope == "conversation":
                return ("not-a-memory",)
            return (memory("g1", "global", 0.9),)

    retriever = ScopedMemoryRetriever(HalfMalformedMemory())

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert [item.memory_id for item in result] == ["g1"]


class RecordingObserver:
    def __init__(self):
        self.observations = []

    def memory_branch_observed(self, branch, outcome, result_count, seconds):
        self.observations.append((branch, outcome, result_count, seconds))


async def test_observer_records_per_branch_success_outcome_and_count():
    observer = RecordingObserver()
    retriever = ScopedMemoryRetriever(
        branch_implementation(
            [],
            conversation=(memory("c1", "local", 0.5), memory("c2", "local 2", 0.7)),
            global_memories=(),
        ),
        observer=observer,
    )

    await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    by_branch = {observation[0]: observation for observation in observer.observations}
    assert by_branch["conversation"][1] == "success"
    assert by_branch["conversation"][2] == 2
    assert by_branch["conversation"][3] >= 0.0
    assert by_branch["global"][1] == "empty"
    assert by_branch["global"][2] == 0
    assert len(observer.observations) == 2


async def test_observer_records_error_outcome_for_failing_branch():
    observer = RecordingObserver()
    retriever = ScopedMemoryRetriever(
        branch_implementation([], error=LongTermMemoryConnectionError()),
        observer=observer,
    )

    with pytest.raises(LongTermMemoryConnectionError):
        await retriever.search(
            "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
        )

    assert {observation[0]: observation[1] for observation in observer.observations} == {
        "conversation": "error",
        "global": "error",
    }
    assert all(observation[2] == 0 for observation in observer.observations)


async def test_observer_records_error_when_protocol_violation_raises():
    class MalformedMemory:
        async def search_scoped(self, user_id, query, *, conversation_id, scope, top_k, threshold):
            return ("not-a-memory",)

    observer = RecordingObserver()
    retriever = ScopedMemoryRetriever(MalformedMemory(), observer=observer)

    with pytest.raises(LongTermMemoryProtocolError):
        await retriever.search(
            "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
        )

    assert {observation[0]: observation[1] for observation in observer.observations} == {
        "conversation": "success",
        "global": "success",
    }


async def test_observer_failures_never_affect_search_results():
    class BrokenObserver:
        def memory_branch_observed(self, branch, outcome, result_count, seconds):
            raise RuntimeError("observer exploded")

    retriever = ScopedMemoryRetriever(
        branch_implementation([], global_memories=(memory("g1", "global", 0.9),)),
        observer=BrokenObserver(),
    )

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert [item.memory_id for item in result] == ["g1"]


async def test_without_observer_search_remains_unchanged():
    retriever = ScopedMemoryRetriever(
        branch_implementation([], conversation=(memory("c1", "local", 0.5),), global_memories=())
    )

    result = await retriever.search(
        "user-1", "query", conversation_id=CONVERSATION_ID, top_k=10, threshold=0.1
    )

    assert [item.memory_id for item in result] == ["c1"]
