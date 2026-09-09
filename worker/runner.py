"""Bounded concurrent polling runtime for PostgreSQL memory jobs."""

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.application.use_cases.process_memory_job import ProcessMemoryJobResult
from app.domain.errors.memory_job import (
    MemoryJobQueueConnectionError,
    MemoryJobQueueError,
    MemoryJobQueueProtocolError,
)
from app.domain.models.memory_job import MemoryJob
from app.domain.ports.memory_job_queue import MemoryJobQueuePort

logger = logging.getLogger(__name__)
_MAX_DATABASE_BACKOFF_SECONDS = 30.0


class ClaimedMemoryJobProcessor(Protocol):
    async def execute(self, job: MemoryJob) -> ProcessMemoryJobResult:
        """Process and transition one currently leased memory job."""
        ...


@dataclass(frozen=True, slots=True)
class MemoryJobRunnerSnapshot:
    """Low-cardinality runtime state for readiness and metrics in T4.14."""

    running: bool
    stopping: bool
    database_available: bool
    last_successful_poll_at: datetime | None
    consecutive_database_failures: int
    database_backoff_seconds: float
    in_flight_count: int


class MemoryJobRunner:
    """Claim only free capacity and drain in-flight work on shutdown."""

    def __init__(
        self,
        memory_job_queue: MemoryJobQueuePort,
        processor: ClaimedMemoryJobProcessor,
        *,
        poll_interval_seconds: float,
        batch_size: int,
        concurrency: int,
        lease_seconds: float,
        max_attempts: int,
        database_timeout_seconds: float,
        shutdown_grace_seconds: float,
        lease_owner: UUID | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_positive_finite(poll_interval_seconds, "poll_interval_seconds")
        _require_positive_integer(batch_size, "batch_size")
        _require_positive_integer(concurrency, "concurrency")
        _require_positive_finite(lease_seconds, "lease_seconds")
        _require_positive_integer(max_attempts, "max_attempts")
        _require_positive_finite(database_timeout_seconds, "database_timeout_seconds")
        _require_positive_finite(shutdown_grace_seconds, "shutdown_grace_seconds")
        if lease_owner is not None and not isinstance(lease_owner, UUID):
            raise ValueError("lease_owner must be a UUID")

        self._queue = memory_job_queue
        self._processor = processor
        self._poll_interval = float(poll_interval_seconds)
        self._batch_size = batch_size
        self._concurrency = concurrency
        self._lease_seconds = float(lease_seconds)
        self._max_attempts = max_attempts
        self._database_timeout = float(database_timeout_seconds)
        self._shutdown_grace = float(shutdown_grace_seconds)
        self._lease_owner = lease_owner or uuid4()
        self._clock = clock or _utc_now

        self._stop_requested = asyncio.Event()
        self._in_flight: set[asyncio.Task[None]] = set()
        self._started = False
        self._running = False
        self._database_available = False
        self._last_successful_poll_at: datetime | None = None
        self._consecutive_database_failures = 0
        self._database_backoff = 0.0

    @property
    def snapshot(self) -> MemoryJobRunnerSnapshot:
        """Return state without queue payload or identity identifiers."""
        return MemoryJobRunnerSnapshot(
            running=self._running,
            stopping=self._stop_requested.is_set(),
            database_available=self._database_available,
            last_successful_poll_at=self._last_successful_poll_at,
            consecutive_database_failures=self._consecutive_database_failures,
            database_backoff_seconds=self._database_backoff,
            in_flight_count=len(self._in_flight),
        )

    def request_stop(self) -> None:
        """Stop new claims; in-flight work receives the configured grace period."""
        self._stop_requested.set()

    async def run(self) -> None:
        """Poll until stopped, isolating per-job failures and honoring free capacity."""
        if self._started:
            raise RuntimeError("memory job runner is single-use")
        self._started = True
        self._running = True
        cancellation: asyncio.CancelledError | None = None
        try:
            while not self._stop_requested.is_set():
                capacity = self._concurrency - len(self._in_flight)
                if capacity <= 0:
                    await self._wait_for_capacity_or_stop()
                    continue

                claim_limit = min(self._batch_size, capacity)
                try:
                    jobs = await self._claim_due(claim_limit)
                except MemoryJobQueueError as error:
                    self._record_database_failure()
                    logger.warning(
                        "Memory job queue poll failed",
                        extra={
                            "dependency": "postgresql",
                            "operation": "claim_memory_jobs",
                            "error_class": type(error).__name__,
                            "fallback_mode": "database_backoff",
                        },
                    )
                    await self._pause(self._database_backoff)
                    continue

                self._record_database_success()
                for job in jobs:
                    task = asyncio.create_task(
                        self._process_job_safely(job),
                        name="memory-job-processing",
                    )
                    self._in_flight.add(task)
                    task.add_done_callback(self._in_flight.discard)

                if not jobs:
                    await self._pause(self._poll_interval)
        except asyncio.CancelledError as error:
            cancellation = error
        finally:
            self._stop_requested.set()
            try:
                await self._drain_in_flight()
            finally:
                self._running = False

        if cancellation is not None:
            raise cancellation

    async def _claim_due(self, limit: int) -> tuple[MemoryJob, ...]:
        try:
            async with asyncio.timeout(self._database_timeout):
                jobs = await self._queue.claim_due(
                    lease_owner=self._lease_owner,
                    limit=limit,
                    lease_seconds=self._lease_seconds,
                    max_attempts=self._max_attempts,
                )
        except TimeoutError:
            raise MemoryJobQueueConnectionError from None
        if (
            not isinstance(jobs, tuple)
            or len(jobs) > limit
            or any(not isinstance(job, MemoryJob) for job in jobs)
        ):
            raise MemoryJobQueueProtocolError
        return jobs

    async def _process_job_safely(self, job: MemoryJob) -> None:
        try:
            await self._processor.execute(job)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Memory job transition failed; lease will be reclaimed",
                extra={
                    "dependency": "memory_job_runtime",
                    "operation": "process_memory_job",
                    "error_class": type(error).__name__,
                    "fallback_mode": "lease_reclaim",
                },
            )

    async def _pause(self, delay_seconds: float) -> None:
        try:
            async with asyncio.timeout(delay_seconds):
                await self._stop_requested.wait()
        except TimeoutError:
            return

    async def _wait_for_capacity_or_stop(self) -> None:
        stop_waiter = asyncio.create_task(self._stop_requested.wait())
        try:
            await asyncio.wait(
                (*self._in_flight, stop_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stop_waiter.done():
                stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)

    async def _drain_in_flight(self) -> None:
        tasks = tuple(self._in_flight)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self._shutdown_grace)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _record_database_failure(self) -> None:
        self._database_available = False
        self._consecutive_database_failures += 1
        self._database_backoff = _database_backoff_seconds(
            self._consecutive_database_failures,
            self._poll_interval,
        )

    def _record_database_success(self) -> None:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        self._database_available = True
        self._last_successful_poll_at = now
        self._consecutive_database_failures = 0
        self._database_backoff = 0.0


def _database_backoff_seconds(consecutive_failures: int, poll_interval_seconds: float) -> float:
    """Return deterministic exponential database backoff capped at 30 seconds."""
    if consecutive_failures < 1:
        return 0.0
    exponent = min(consecutive_failures - 1, 30)
    cap = max(_MAX_DATABASE_BACKOFF_SECONDS, poll_interval_seconds)
    return min(cap, poll_interval_seconds * (2**exponent))


def _require_positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_positive_finite(value: object, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be finite and positive")


def _utc_now() -> datetime:
    return datetime.now(UTC)
