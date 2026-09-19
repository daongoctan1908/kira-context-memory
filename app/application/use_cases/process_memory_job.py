"""Classify and persist the outcome of one leased memory-formation job."""

import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.domain.errors.conversation import (
    ConversationSourceUnavailableError,
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
    LongTermMemorySourceUnavailableError,
    LongTermMemoryTimeoutError,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory import MemoryProcessResult
from app.domain.models.memory_job import MemoryJob
from app.domain.ports.context_observer import StageObservationPort
from app.domain.ports.memory_job_observer import MemoryJobProcessObserverPort
from app.domain.ports.memory_job_queue import MemoryJobQueuePort


class _NoOpObservation:
    def set_attribute(self, key: str, value: object) -> None:
        return None

    def set_outcome(self, outcome: str) -> None:
        return None


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
_SOURCE_UNAVAILABLE_ERRORS = (
    ConversationSourceUnavailableError,
    LongTermMemorySourceUnavailableError,
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
    SKIPPED = "skipped"
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
        observer: MemoryJobProcessObserverPort | None = None,
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
        self._observer = observer

    async def execute(self, job: MemoryJob) -> ProcessMemoryJobResult:
        """Process one current lease and complete, retry, or dead-letter it."""
        if not isinstance(job, MemoryJob):
            raise TypeError("job must be a MemoryJob")
        if job.attempt_count > self._max_attempts:
            raise ValueError("job attempt exceeds the configured maximum")

        try:
            result = await self._process_memory.execute(job.reference, job.event_id)
        except _SOURCE_UNAVAILABLE_ERRORS:
            return await self._skip(job)
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
        with self._transition_stage("complete") as observation:
            try:
                await self._queue.complete(
                    job.event_id,
                    job.lease_token,
                    lifecycle_event_count=lifecycle_event_count,
                )
            except BaseException:
                _observe(observation.set_outcome, "error")
                raise
            _observe(observation.set_outcome, "completed")
        return ProcessMemoryJobResult(
            outcome=MemoryJobProcessOutcome.COMPLETED,
            lifecycle_event_count=lifecycle_event_count,
        )

    async def _skip(self, job: MemoryJob) -> ProcessMemoryJobResult:
        """Terminally acknowledge a job whose source can no longer own memories."""
        with self._transition_stage("skip") as observation:
            try:
                await self._queue.complete(
                    job.event_id,
                    job.lease_token,
                    lifecycle_event_count=0,
                )
            except BaseException:
                _observe(observation.set_outcome, "error")
                raise
            _observe(observation.set_outcome, "skipped")
        return ProcessMemoryJobResult(outcome=MemoryJobProcessOutcome.SKIPPED)

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
        with self._transition_stage("retry") as observation:
            try:
                await self._queue.retry(
                    job.event_id,
                    job.lease_token,
                    next_attempt_at=next_attempt_at,
                    error_class=error_class,
                )
            except BaseException:
                _observe(observation.set_outcome, "error")
                raise
            _observe(observation.set_outcome, "retry")
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
        with self._transition_stage("dead") as observation:
            try:
                await self._queue.dead_letter(
                    job.event_id,
                    job.lease_token,
                    error_class=error_class,
                )
            except BaseException:
                _observe(observation.set_outcome, "error")
                raise
            _observe(observation.set_outcome, "dead")
        return ProcessMemoryJobResult(
            outcome=MemoryJobProcessOutcome.DEAD,
            error_class=error_class,
        )

    @contextmanager
    def _transition_stage(self, transition: str) -> Iterator[StageObservationPort]:
        if self._observer is None:
            yield _NoOpObservation()
            return
        try:
            manager = self._observer.stage(
                "memory_job.transition",
                kind="client",
                attributes={"kira.memory.job.transition": transition},
            )
            observation = manager.__enter__()
        except Exception:
            yield _NoOpObservation()
            return
        try:
            yield observation
        except BaseException as error:
            try:
                manager.__exit__(type(error), error, error.__traceback__)
            except Exception:
                pass
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


def _observe(operation, *args: object) -> None:
    try:
        operation(*args)
    except Exception:
        pass


def _utc_now() -> datetime:
    return datetime.now(UTC)
