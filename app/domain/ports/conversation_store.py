"""Port for ordered short-term conversation persistence."""

from typing import Protocol
from uuid import UUID

from app.domain.models.conversation import (
    AppendTurnResult,
    ConversationHistoryPage,
    ConversationListCursor,
    ConversationMessage,
    ConversationPage,
    ConversationSummary,
)
from app.domain.models.telemetry_context import TelemetryContext


class ConversationStorePort(Protocol):
    """Application-facing abstraction for recent completed conversation turns."""

    async def create_conversation(
        self,
        user_id: str,
        *,
        title: str | None = None,
    ) -> ConversationSummary:
        """Create an active conversation with a server-generated public ID."""
        ...

    async def list_conversations(
        self,
        user_id: str,
        *,
        limit: int,
        cursor: ConversationListCursor | None = None,
    ) -> ConversationPage:
        """List owned conversations in stable descending activity order."""
        ...

    async def read_history(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int,
        before_message_id: int | None = None,
    ) -> ConversationHistoryPage | None:
        """Read an active owned conversation, or return ``None`` when unavailable."""
        ...

    async def mark_deletion_pending(self, user_id: str, session_id: str) -> bool:
        """Mark an owned conversation for deletion, returning false when not found."""
        ...

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
        *,
        schedule_memory: bool = False,
        telemetry_context: TelemetryContext | None = None,
    ) -> AppendTurnResult:
        """Atomically append a completed turn and its optional memory job."""
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
