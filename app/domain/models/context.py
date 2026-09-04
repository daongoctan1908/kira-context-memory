"""Immutable input to the query-rewriting capability."""

from dataclasses import dataclass, field

from app.domain.models.conversation import ConversationMessage


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Recent history and the untouched current query, with an estimated budget."""

    recent_messages: tuple[ConversationMessage, ...] = field(repr=False)
    current_query: str = field(repr=False)
    estimated_recent_tokens: int

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
