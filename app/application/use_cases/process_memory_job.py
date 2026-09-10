"""Classify and persist the outcome of one leased memory-formation job."""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.errors.memory import (
    LongTermMemoryConfigurationError,
    LongTermMemoryConnectionError,
    LongTermMemoryOperationError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory import MemoryProcessResult
from app.domain.models.memory_job import MemoryJob
from app.domain.ports.memory_job_queue import MemoryJobQueuePort

_RETRYABLE_ERRORS = (
    ConversationStoreConnectionError,
    ConversationStoreOperationError,
    LongTermMemoryConnectionError,
    LongTermMemoryOperationError,
    LongTermMemoryTimeoutError,
)
_PERMANENT_ERRORS = (
    ConversationStoreConfigurationError,
    ConversationStoreProtocolError,
    LongTermMemoryConfigurationError,
    LongTermMemoryProtocolError,
)


class MemoryProcessor(Protocol):
    async def execute(
        self,
        reference: CompletedTurnReference,
        formation_event_id: UUID,
    ) -> MemoryProcessResult:
        """Form memory from the exact persisted boundary."""
        ...


class MemoryJobProcessOutcome(StrEnum):
    """Low-cardinality result of one claimed-job delivery."""

    COMPLETED = "completed"
    RETRY = "retry"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class ProcessMemoryJobResult:
    """Sanitized execution result suitable for later Worker metrics and logs."""

    outcome: MemoryJobProcessOutcome
    lifecycle_event_count: int = 0
    error_class: str | None = None
    next_attempt_at: datetime | None = None


class ProcessMemoryJobUseCase:
    """Run one leased boundary and record exactly one queue transition."""

    def __init__(
        self,
        process_memory: MemoryProcessor,
        memory_job_queue: MemoryJobQueuePort,
        *,
        max_attempts: int,
        retry_delays_seconds: tuple[float, ...],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if len(retry_delays_seconds) != max_attempts - 1 or any(
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or delay <= 0
            for delay in retry_delays_seconds
        ):
            raise ValueError("retry delays must contain one finite positive delay per retry")
        self._process_memory = process_memory
        self._queue = memory_job_queue
        self._max_attempts = max_attempts
        self._retry_delays = tuple(float(delay) for delay in retry_delays_seconds)
        self._clock = clock or _utc_now

    async def execute(self, job: MemoryJob) -> ProcessMemoryJobResult:
        """Process one current lease and complete, retry, or dead-letter it."""
        if not isinstance(job, MemoryJob):
            raise TypeError("job must be a MemoryJob")
        if job.attempt_count > self._max_attempts:
            raise ValueError("job attempt exceeds the configured maximum")

        try:
            result = await self._process_memory.execute(job.reference, job.event_id)
        except _PERMANENT_ERRORS as error:
            return await self._dead_letter(job, error)
        except _RETRYABLE_ERRORS as error:
            if job.attempt_count >= self._max_attempts:
                return await self._dead_letter(job, error)
            return await self._retry(job, error)
        except Exception as error:
            if job.attempt_count >= self._max_attempts:
                return await self._dead_letter(job, error)
            return await self._retry(job, error)

        lifecycle_event_count = len(result.events)
        await self._queue.complete(
            job.event_id,
            job.lease_token,
            lifecycle_event_count=lifecycle_event_count,
        )
        return ProcessMemoryJobResult(
            outcome=MemoryJobProcessOutcome.COMPLETED,
            lifecycle_event_count=lifecycle_event_count,
        )

    async def _retry(
        self,
        job: MemoryJob,
        error: Exception,
    ) -> ProcessMemoryJobResult:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        next_attempt_at = now + timedelta(seconds=self._retry_delays[job.attempt_count - 1])
        error_class = type(error).__name__
        await self._queue.retry(
            job.event_id,
            job.lease_token,
            next_attempt_at=next_attempt_at,
            error_class=error_class,
        )
        return ProcessMemoryJobResult(
            outcome=MemoryJobProcessOutcome.RETRY,
            error_class=error_class,
            next_attempt_at=next_attempt_at,
        )

    async def _dead_letter(
        self,
        job: MemoryJob,
        error: Exception,
    ) -> ProcessMemoryJobResult:
        error_class = type(error).__name__
        await self._queue.dead_letter(
            job.event_id,
            job.lease_token,
            error_class=error_class,
        )
        return ProcessMemoryJobResult(
            outcome=MemoryJobProcessOutcome.DEAD,
            error_class=error_class,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
