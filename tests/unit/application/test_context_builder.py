from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from app.application.services.context_builder import ContextBuilder, estimate_recent_tokens
from app.domain.models.context import ConversationContext
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory import LongTermMemory


def pair(turn: int, content: str = "text") -> tuple[ConversationMessage, ConversationMessage]:
    return tuple(
        ConversationMessage(
            session_id="session-1",
            turn_id=f"turn-{turn}",
            role=role,
            content=content,
            timestamp=datetime(2026, 9, 4, tzinfo=UTC) + timedelta(seconds=turn * 2 + index),
        )
        for index, role in enumerate((ConversationRole.USER, ConversationRole.ASSISTANT))
    )


def memory(index: int, content: str | None = None) -> LongTermMemory:
    return LongTermMemory(
        memory_id=f"memory-{index}",
        content=content or f"ranked fact {index}",
        score=1 - index / 100,
        metadata={"private": f"metadata-{index}"},
    )


@pytest.mark.parametrize("content,tokens", [("a", 9), ("abcd", 9), ("abcde", 10), ("ế", 9)])
def test_token_estimator_is_deterministic_utf8_heuristic(content: str, tokens: int) -> None:
    assert estimate_recent_tokens(pair(1, content)) == tokens * 2
    assert estimate_recent_tokens(()) == 0


def test_empty_history_keeps_current_query_untouched() -> None:
    query = "  Hưng Yên thì sao?  "
    context = ContextBuilder().build([], query)

    assert context.recent_messages == ()
    assert context.current_query == query
    assert context.estimated_recent_tokens == 0
    assert context.long_term_memories == ()


def test_ltm_ranking_is_preserved_and_bounded_independently_from_recent() -> None:
    ranked = tuple(memory(index) for index in range(12))
    recent = pair(1, "x" * 100)

    context = ContextBuilder(recent_token_budget=10).build(recent, "current", ranked)

    assert context.long_term_memories == ranked[:10]
    assert context.recent_messages == ()
    assert context.estimated_recent_tokens == 0


def test_configured_ltm_cap_keeps_provider_order_without_sorting_or_deduplication() -> None:
    first = memory(1)
    second = memory(2)
    ranked = (second, first, second)

    context = ContextBuilder(max_long_term_memories=2).build([], "q", ranked)

    assert context.long_term_memories == (second, first)


def test_ltm_has_no_token_budget_and_never_changes_current_query() -> None:
    current_query = "  explicit current query  "
    large_memories = tuple(memory(index, "nội dung " * 4000) for index in range(10))

    context = ContextBuilder(recent_token_budget=1).build([], current_query, large_memories)

    assert context.long_term_memories == large_memories
    assert context.estimated_recent_tokens == 0
    assert context.current_query == current_query


def test_message_cap_keeps_newest_messages_without_mutating_input() -> None:
    history = [message for turn in range(7) for message in pair(turn)]
    before = history.copy()

    context = ContextBuilder().build(history, "current")

    assert len(context.recent_messages) == 10
    assert context.recent_messages == tuple(history[-10:])
    assert context.estimated_recent_tokens == estimate_recent_tokens(history[-10:])
    assert history == before


def test_budget_exact_boundary_includes_the_whole_pair() -> None:
    history = pair(1) + pair(2)
    budget = estimate_recent_tokens(history)

    assert ContextBuilder(recent_token_budget=budget).build(history, "q").recent_messages == history
    trimmed = ContextBuilder(recent_token_budget=budget - 1).build(history, "q")
    assert trimmed.recent_messages == pair(2)
    assert trimmed.estimated_recent_tokens == estimate_recent_tokens(pair(2))


def test_budget_drops_oldest_turns_and_restores_store_order_not_timestamp_order() -> None:
    history = pair(1) + pair(2) + pair(3)
    history = tuple(
        replace(message, timestamp=datetime(2026, 9, 4, tzinfo=UTC) - timedelta(seconds=index))
        for index, message in enumerate(history)
    )

    context = ContextBuilder(recent_token_budget=36).build(history, "q")

    assert context.recent_messages == history[-4:]
    assert context.estimated_recent_tokens == 36


def test_oversized_latest_turn_leaves_empty_history_and_never_trims_current() -> None:
    current_query = "nội dung hiện tại " * 4000
    context = ContextBuilder(recent_token_budget=10).build(pair(1, "x" * 100), current_query)

    assert context.recent_messages == ()
    assert context.estimated_recent_tokens == 0
    assert context.current_query == current_query


def test_cap_cutting_a_turn_drops_the_oldest_orphan_assistant() -> None:
    history = pair(1) + pair(2) + (pair(3)[0],)
    context = ContextBuilder(max_recent_messages=4).build(history, "q")

    assert context.recent_messages == history[2:]
    assert context.recent_messages[0].role is ConversationRole.USER


def test_assistant_only_window_is_discarded() -> None:
    context = ContextBuilder().build((pair(1)[1],), "q")
    assert context.recent_messages == ()


def test_single_message_can_be_removed_before_a_newer_complete_turn() -> None:
    history = (pair(1)[0],) + pair(2)
    context = ContextBuilder(recent_token_budget=18).build(history, "q")
    assert context.recent_messages == pair(2)


def test_builder_rejects_cross_session_history() -> None:
    history = pair(1) + tuple(replace(message, session_id="another") for message in pair(2))
    with pytest.raises(ValueError, match="one session"):
        ContextBuilder().build(history, "q")


def test_window_invariants_across_history_lengths_caps_and_budgets() -> None:
    for turn_count in range(9):
        history = tuple(
            message
            for turn in range(turn_count)
            for message in pair(turn, "nội dung " * (turn + 1))
        )
        for cap in (2, 4, 10):
            for budget in (1, 18, 50, 150, 3000):
                context = ContextBuilder(max_recent_messages=cap, recent_token_budget=budget).build(
                    history, "current query"
                )
                kept = context.recent_messages
                assert len(kept) <= cap
                assert len(kept) % 2 == 0
                assert context.estimated_recent_tokens == estimate_recent_tokens(kept)
                assert context.estimated_recent_tokens <= budget
                assert context.current_query == "current query"
                if kept:
                    assert kept == history[-len(kept) :]
                    for index in range(0, len(kept), 2):
                        assert kept[index].turn_id == kept[index + 1].turn_id


@pytest.mark.parametrize("limit", [0, 1, 3, -2, True, 2.5])
def test_builder_rejects_invalid_message_cap(limit: int) -> None:
    with pytest.raises(ValueError):
        ContextBuilder(max_recent_messages=limit)


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_builder_rejects_invalid_budget(budget: int) -> None:
    with pytest.raises(ValueError):
        ContextBuilder(recent_token_budget=budget)


@pytest.mark.parametrize("limit", [0, 11, -1, True, 1.5])
def test_builder_rejects_invalid_ltm_cap(limit: int) -> None:
    with pytest.raises(ValueError, match="max_long_term_memories"):
        ContextBuilder(max_long_term_memories=limit)


def test_context_is_immutable_and_repr_does_not_contain_conversation_text() -> None:
    context = ContextBuilder().build(
        pair(1, "private-history"),
        "private-current",
        (memory(1, "private-memory"),),
    )
    assert "private" not in repr(context)
    with pytest.raises(FrozenInstanceError):
        context.current_query = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("query", ["", "  ", None, 123])
def test_context_rejects_empty_or_non_string_current_query(query: str) -> None:
    with pytest.raises(ValueError):
        ConversationContext((), query, 0)


@pytest.mark.parametrize("tokens", [-1, True, 1.5])
def test_context_rejects_invalid_estimated_count(tokens: int) -> None:
    with pytest.raises(ValueError):
        ConversationContext((), "query", tokens)


@pytest.mark.parametrize("messages", [[], ("not-a-message",)])
def test_context_requires_immutable_typed_messages(messages) -> None:
    with pytest.raises(ValueError):
        ConversationContext(messages, "query", 0)


@pytest.mark.parametrize("memories", [[], ("not-a-memory",)])
def test_context_requires_immutable_typed_ltm(memories) -> None:
    with pytest.raises(ValueError, match="long_term_memories"):
        ConversationContext((), "query", 0, memories)


def test_context_rejects_more_than_ten_ltm_items() -> None:
    with pytest.raises(ValueError, match="at most 10"):
        ConversationContext((), "query", 0, tuple(memory(index) for index in range(11)))
