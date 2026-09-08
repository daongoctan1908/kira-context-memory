"""Immutable input to the query-rewriting capability."""

from dataclasses import dataclass, field

from app.domain.models.conversation import ConversationMessage
from app.domain.models.memory import LongTermMemory

MAX_LONG_TERM_MEMORIES = 10


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Ranked LTM, bounded recent history, and the untouched current query."""

    recent_messages: tuple[ConversationMessage, ...] = field(repr=False)
    current_query: str = field(repr=False)
    estimated_recent_tokens: int
    long_term_memories: tuple[LongTermMemory, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.recent_messages, tuple) or any(
            not isinstance(message, ConversationMessage) for message in self.recent_messages
        ):
            raise ValueError("recent_messages must be an immutable tuple of messages")
        if not isinstance(self.current_query, str) or not self.current_query.strip():
            raise ValueError("current_query must not be empty")
        if (
            isinstance(self.estimated_recent_tokens, bool)
            or not isinstance(self.estimated_recent_tokens, int)
            or self.estimated_recent_tokens < 0
        ):
            raise ValueError("estimated_recent_tokens must be a nonnegative integer")
        if not isinstance(self.long_term_memories, tuple) or any(
            not isinstance(memory, LongTermMemory) for memory in self.long_term_memories
        ):
            raise ValueError("long_term_memories must be an immutable tuple of memories")
        if len(self.long_term_memories) > MAX_LONG_TERM_MEMORIES:
            raise ValueError(
                f"long_term_memories must contain at most {MAX_LONG_TERM_MEMORIES} items"
            )
