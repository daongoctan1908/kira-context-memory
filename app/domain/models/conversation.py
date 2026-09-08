"""Versioned short-term conversation models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

CONVERSATION_MESSAGE_SCHEMA_VERSION = 1


class ConversationRole(StrEnum):
    """Roles persisted in the recent-conversation store."""

    USER = "user"
    ASSISTANT = "assistant"


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
