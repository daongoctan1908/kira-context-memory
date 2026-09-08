"""Bound recent history without modifying the user's current query."""

from collections.abc import Sequence
from itertools import groupby

from app.domain.models.context import MAX_LONG_TERM_MEMORIES, ConversationContext
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.memory import LongTermMemory


def estimate_recent_tokens(messages: Sequence[ConversationMessage]) -> int:
    """Estimate tokens as ceil(UTF-8 bytes / 4) + 8 overhead per message.

    This deterministic heuristic is not a Qwen tokenizer. Only recent-message
    content/overhead is counted; the system prompt and current query are separate.
    """
    return sum((len(message.content.encode("utf-8")) + 3) // 4 + 8 for message in messages)


class ContextBuilder:
    """Keep the newest bounded window, removing oldest turns before newer ones."""

    def __init__(
        self,
        *,
        max_recent_messages: int = 10,
        recent_token_budget: int = 3000,
        max_long_term_memories: int = MAX_LONG_TERM_MEMORIES,
    ) -> None:
        if (
            isinstance(max_recent_messages, bool)
            or not isinstance(max_recent_messages, int)
            or max_recent_messages < 2
            or max_recent_messages % 2
        ):
            raise ValueError("max_recent_messages must be a positive even number")
        if (
            isinstance(recent_token_budget, bool)
            or not isinstance(recent_token_budget, int)
            or recent_token_budget < 1
        ):
            raise ValueError("recent_token_budget must be a positive integer")
        if (
            isinstance(max_long_term_memories, bool)
            or not isinstance(max_long_term_memories, int)
            or not 1 <= max_long_term_memories <= MAX_LONG_TERM_MEMORIES
        ):
            raise ValueError(
                f"max_long_term_memories must be between 1 and {MAX_LONG_TERM_MEMORIES}"
            )
        self._max_recent_messages = max_recent_messages
        self._recent_token_budget = recent_token_budget
        self._max_long_term_memories = max_long_term_memories

    def build(
        self,
        recent_messages: Sequence[ConversationMessage],
        current_query: str,
        long_term_memories: Sequence[LongTermMemory] = (),
    ) -> ConversationContext:
        """Use store order (oldest to newest), not wall-clock timestamp order.

        A capped window may start with the assistant half of an older turn; that
        orphan is discarded. Remaining contiguous turns are dropped as units.
        An oversized latest turn leaves empty history, never a truncated query.
        """
        if len({message.session_id for message in recent_messages}) > 1:
            raise ValueError("recent messages must belong to one session")

        recent = tuple(recent_messages[-self._max_recent_messages :])
        if recent and recent[0].role is ConversationRole.ASSISTANT:
            recent = recent[1:]

        turns = [tuple(group) for _, group in groupby(recent, key=lambda message: message.turn_id)]
        turn_tokens = [estimate_recent_tokens(turn) for turn in turns]
        estimated_tokens = sum(turn_tokens)
        first = 0
        while estimated_tokens > self._recent_token_budget:
            estimated_tokens -= turn_tokens[first]
            first += 1

        return ConversationContext(
            recent_messages=tuple(message for turn in turns[first:] for message in turn),
            current_query=current_query,
            estimated_recent_tokens=estimated_tokens,
            long_term_memories=tuple(long_term_memories[: self._max_long_term_memories]),
        )
