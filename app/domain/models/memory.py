"""Framework-free long-term-memory models."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)


@dataclass(frozen=True, slots=True)
class LongTermMemory:
    """One user-scoped memory returned by semantic search."""

    memory_id: str
    content: str
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not self.memory_id.strip():
            raise ValueError("memory_id must not be empty")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("memory content must not be empty")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not 0 <= self.score <= 1
        ):
            raise ValueError("memory score must be between zero and one")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("memory metadata must be a mapping")


@dataclass(frozen=True, slots=True)
class MemorySource:
    """Exact persisted conversation boundary supplied to memory formation."""

    reference: CompletedTurnReference
    messages: tuple[ConversationMessage, ...]

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("memory source messages must not be empty")
        if any(message.session_id != self.reference.session_id for message in self.messages):
            raise ValueError("memory source messages must match the referenced session")
        boundary = self.messages[-1]
        if (
            boundary.role is not ConversationRole.ASSISTANT
            or boundary.turn_id != self.reference.turn_id
        ):
            raise ValueError("memory source must end at the referenced assistant turn")


@dataclass(frozen=True, slots=True)
class MemoryProcessResult:
    """ADD-only formation outcome; this is not the legacy Mem0 action engine."""

    added_memory_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in self.added_memory_ids):
            raise ValueError("added memory IDs must not be empty")
        if len(set(self.added_memory_ids)) != len(self.added_memory_ids):
            raise ValueError("added memory IDs must be unique")

    @property
    def added(self) -> bool:
        return bool(self.added_memory_ids)
