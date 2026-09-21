"""Port for ordered short-term conversation persistence."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.models.conversation import (
    AppendTurnResult,
    ChatRequestReservation,
    ChatRequestStatus,
    ConversationHistoryPage,
    ConversationListCursor,
    ConversationMessage,
    ConversationPage,
    ConversationSummary,
    MessageFeedbackRating,
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
        query: str | None = None,
    ) -> ConversationPage:
        """List owned conversations in stable descending activity order."""
        ...

    async def rename_conversation(
        self,
        user_id: str,
        session_id: str,
        title: str,
    ) -> ConversationSummary | None:
        """Rename one active owned conversation, or return ``None`` when unavailable."""
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

    async def set_message_feedback(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        rating: MessageFeedbackRating,
    ) -> bool:
        """Upsert feedback for one persisted assistant turn, returning false when absent."""
        ...

    async def clear_message_feedback(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
    ) -> bool:
        """Idempotently clear feedback, returning false only when the turn is unavailable."""
        ...

    async def mark_deletion_pending(self, user_id: str, session_id: str) -> bool:
        """Mark an owned conversation for deletion, returning false when not found."""
        ...

    async def purge_deletion_pending(self, user_id: str, session_id: str) -> bool:
        """Atomically erase one owned pending conversation and its memory provenance."""
        ...

    async def is_conversation_active(self, user_id: str, session_id: str) -> bool:
        """Return whether the owned conversation still accepts context and writes."""
        ...

    async def reserve_chat_request(
        self,
        user_id: str,
        session_id: str,
        client_message_id: UUID,
        content_hash: bytes,
        *,
        now: datetime,
        lease_seconds: float,
    ) -> ChatRequestReservation | None:
        """Acquire/replay/reclaim a fenced request for an active owned conversation."""
        ...

    async def abandon_chat_request(
        self,
        request_id: UUID,
        lease_token: UUID,
        *,
        status: ChatRequestStatus,
        now: datetime,
    ) -> bool:
        """Release a processing attempt only when its lease token is still current."""
        ...

    async def complete_chat_request(
        self,
        user_id: str,
        reservation: ChatRequestReservation,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        *,
        completed_at: datetime,
        schedule_memory: bool = False,
        telemetry_context: TelemetryContext | None = None,
    ) -> AppendTurnResult:
        """Atomically persist a fenced request, completed turn and optional memory job."""
        ...

    async def read_completed_chat_request(
        self,
        user_id: str,
        session_id: str,
        reservation: ChatRequestReservation,
    ) -> tuple[ConversationMessage, ConversationMessage] | None:
        """Read the persisted pair for an owned completed request without calling KiRa."""
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
