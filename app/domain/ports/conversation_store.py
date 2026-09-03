"""Port for ordered short-term conversation persistence."""

from typing import Protocol

from app.domain.models.conversation import ConversationMessage


class ConversationStorePort(Protocol):
    """Application-facing abstraction for recent completed conversation turns."""

    async def read_recent(
        self,
        session_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Read at most ``limit`` recent messages in chronological order."""
        ...

    async def append_turn(
        self,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
    ) -> bool:
        """Atomically append one completed turn; return false when already stored."""
        ...
