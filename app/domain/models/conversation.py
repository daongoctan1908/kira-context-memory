"""Versioned short-term conversation models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

CONVERSATION_MESSAGE_SCHEMA_VERSION = 1
MAX_CONVERSATION_TITLE_LENGTH = 200


class ConversationRole(StrEnum):
    """Roles persisted in the recent-conversation store."""

    USER = "user"
    ASSISTANT = "assistant"


class ConversationStatus(StrEnum):
    """Lifecycle states visible to conversation management."""

    ACTIVE = "active"
    DELETION_PENDING = "deletion_pending"


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """Owned conversation metadata returned by create/list operations."""

    conversation_id: UUID
    session_id: str
    title: str | None
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_id, UUID):
            raise ValueError("conversation_id must be a UUID")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if self.title is not None:
            if not isinstance(self.title, str) or (
                not self.title.strip()
                or self.title != self.title.strip()
                or len(self.title) > MAX_CONVERSATION_TITLE_LENGTH
            ):
                raise ValueError("title must be trimmed and within the supported length")
        if not isinstance(self.status, ConversationStatus):
            raise ValueError("status must be a supported conversation status")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.last_message_at is not None:
            _aware(self.last_message_at, "last_message_at")

    @property
    def activity_at(self) -> datetime:
        return self.last_message_at or self.created_at


@dataclass(frozen=True, slots=True)
class ConversationListCursor:
    """Stable keyset cursor matching the conversation list ordering."""

    activity_at: datetime
    conversation_id: UUID

    def __post_init__(self) -> None:
        _aware(self.activity_at, "activity_at")
        if not isinstance(self.conversation_id, UUID):
            raise ValueError("conversation_id must be a UUID")


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """One bounded page of owned conversation summaries."""

    items: tuple[ConversationSummary, ...]
    next_cursor: ConversationListCursor | None

    def __post_init__(self) -> None:
        if not all(isinstance(item, ConversationSummary) for item in self.items):
            raise ValueError("conversation page items must be summaries")
        if self.next_cursor is not None and not isinstance(
            self.next_cursor, ConversationListCursor
        ):
            raise ValueError("next_cursor must be a conversation list cursor")


@dataclass(frozen=True, slots=True)
class ConversationHistoryPage:
    """Chronological message page with a cursor for older messages."""

    messages: tuple["ConversationMessage", ...]
    next_before_message_id: int | None

    def __post_init__(self) -> None:
        if not all(isinstance(message, ConversationMessage) for message in self.messages):
            raise ValueError("history page items must be conversation messages")
        if self.next_before_message_id is not None and (
            isinstance(self.next_before_message_id, bool)
            or not isinstance(self.next_before_message_id, int)
            or self.next_before_message_id < 1
        ):
            raise ValueError("history cursor must be a positive message ID")


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One immutable, versioned message in a completed conversation turn."""

    session_id: str
    turn_id: str
    role: ConversationRole
    content: str
    timestamp: datetime
    schema_version: int = CONVERSATION_MESSAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.role, ConversationRole):
            raise ValueError("role must be a supported conversation role")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content must not be empty")
        if (
            not isinstance(self.timestamp, datetime)
            or self.timestamp.tzinfo is None
            or self.timestamp.utcoffset() is None
        ):
            raise ValueError("timestamp must be timezone-aware")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CONVERSATION_MESSAGE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported conversation message schema version")


@dataclass(frozen=True, slots=True)
class CompletedTurnReference:
    """Stable database boundary for one fully persisted user/assistant turn."""

    user_id: str
    session_id: str
    conversation_id: UUID
    turn_id: str
    boundary_message_id: int

    def __post_init__(self) -> None:
        for field_name in ("user_id", "session_id", "turn_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if not isinstance(self.conversation_id, UUID):
            raise ValueError("conversation_id must be a UUID")
        if (
            isinstance(self.boundary_message_id, bool)
            or not isinstance(self.boundary_message_id, int)
            or self.boundary_message_id < 1
        ):
            raise ValueError("boundary_message_id must be positive")


@dataclass(frozen=True, slots=True)
class AppendTurnResult:
    """Transactional append outcome including its boundary and optional memory job."""

    inserted: bool
    reference: CompletedTurnReference
    memory_job_event_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.inserted, bool):
            raise ValueError("inserted must be a boolean")
        if not isinstance(self.reference, CompletedTurnReference):
            raise ValueError("reference must be a completed turn reference")
        if self.memory_job_event_id is not None and not isinstance(self.memory_job_event_id, UUID):
            raise ValueError("memory_job_event_id must be a UUID when present")
