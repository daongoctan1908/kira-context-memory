"""Application boundary for user-scoped long-term memory."""

from typing import Literal, Protocol
from uuid import UUID

from app.domain.models.memory import LongTermMemory, MemoryProcessResult, MemorySource

MemoryScopeBranch = Literal["conversation", "global"]


class LongTermMemoryPort(Protocol):
    async def search(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        """Search only memories owned by ``user_id``."""
        ...

    async def search_scoped(
        self,
        user_id: str,
        query: str,
        *,
        conversation_id: UUID,
        scope: MemoryScopeBranch,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        """Search one scope branch: conversation-local or user-global memories."""
        ...

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        """Return the ordered lifecycle outcome for one exact persisted source."""
        ...
