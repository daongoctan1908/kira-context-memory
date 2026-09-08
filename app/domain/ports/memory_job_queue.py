"""Application boundary for durable asynchronous memory-job processing."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.models.memory_job import (
    DeadMemoryJob,
    MemoryJob,
    MemoryJobPurgeResult,
    MemoryJobStats,
)


class MemoryJobQueuePort(Protocol):
    """Claim and transition jobs without exposing the queue implementation."""

    async def claim_due(
        self,
        *,
        lease_owner: UUID,
        limit: int,
        lease_seconds: float,
        max_attempts: int,
    ) -> tuple[MemoryJob, ...]:
        """Lease due or abandoned jobs in deterministic queue order."""
        ...

    async def complete(
        self,
        event_id: UUID,
        lease_token: UUID,
        *,
        lifecycle_event_count: int,
    ) -> None:
        """Complete a job only while the caller owns its current lease."""
        ...

    async def retry(
        self,
        event_id: UUID,
        lease_token: UUID,
        *,
        next_attempt_at: datetime,
        error_class: str,
    ) -> None:
        """Release a leased job back to the pending queue after a bounded delay."""
        ...

    async def dead_letter(
        self,
        event_id: UUID,
        lease_token: UUID,
        *,
        error_class: str,
    ) -> None:
        """Move a leased job to the terminal dead-letter state."""
        ...

    async def stats(self) -> MemoryJobStats:
        """Return aggregate queue state without conversation identifiers or content."""
        ...

    async def list_dead(self, *, limit: int) -> tuple[DeadMemoryJob, ...]:
        """Return a bounded sanitized dead-job projection for operators."""
        ...

    async def requeue_dead(self, event_id: UUID) -> bool:
        """Requeue exactly one dead event and reset its processing attempt count."""
        ...

    async def purge_terminal(
        self,
        *,
        completed_before: datetime,
        dead_before: datetime,
        limit: int,
    ) -> MemoryJobPurgeResult:
        """Delete bounded expired completed/dead rows; never pending/processing rows."""
        ...
