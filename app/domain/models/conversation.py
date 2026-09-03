"""Versioned short-term conversation models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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
