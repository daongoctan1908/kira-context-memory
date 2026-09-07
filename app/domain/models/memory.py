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
class MemoryLifecycleEvent:
    """One provider-neutral lifecycle result returned by memory formation."""

    action: str
    memory_id: str | None = None
    content: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not self.action.strip():
            raise ValueError("memory lifecycle action must not be empty")
        for field_name in ("memory_id", "content"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be a non-empty string when present")


@dataclass(frozen=True, slots=True)
class MemoryProcessResult:
    """Ordered lifecycle events produced by one memory-formation operation."""

    events: tuple[MemoryLifecycleEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, MemoryLifecycleEvent) for event in self.events
        ):
            raise ValueError("events must be a tuple of memory lifecycle events")

    @property
    def added_memory_ids(self) -> tuple[str, ...]:
        """Return ADD identifiers for compatibility with the original A1 contract."""
        return tuple(
            event.memory_id
            for event in self.events
            if event.action == "ADD" and event.memory_id is not None
        )

    @property
    def added(self) -> bool:
        return bool(self.added_memory_ids)
