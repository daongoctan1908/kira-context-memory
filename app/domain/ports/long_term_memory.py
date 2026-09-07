"""Application boundary for user-scoped long-term memory."""

from typing import Protocol

from app.domain.models.memory import LongTermMemory, MemoryProcessResult, MemorySource


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

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        """Return the ordered lifecycle outcome for one exact persisted source."""
        ...
