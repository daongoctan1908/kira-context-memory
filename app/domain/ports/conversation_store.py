"""Port for ordered short-term conversation persistence."""

from typing import Protocol
from uuid import UUID

from app.domain.models.conversation import AppendTurnResult, ConversationMessage


class ConversationStorePort(Protocol):
    """Application-facing abstraction for recent completed conversation turns."""

    async def read_recent(
        self,
        user_id: str,
        session_id: str,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Read at most ``limit`` recent messages in chronological order."""
        ...

    async def append_turn(
        self,
        user_id: str,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
    ) -> AppendTurnResult:
        """Atomically append one completed turn and return its stable boundary."""
        ...

    async def read_through_boundary(
        self,
        user_id: str,
        conversation_id: UUID,
        boundary_message_id: int,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """Read a chronological window ending at an exact assistant boundary."""
        ...
