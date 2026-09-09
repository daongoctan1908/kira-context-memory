"""Bounded retention cleanup for terminal PostgreSQL memory jobs."""

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.domain.errors.memory_job import MemoryJobQueueProtocolError
from app.domain.models.memory_job import MemoryJobPurgeResult
from app.domain.ports.memory_job_queue import MemoryJobQueuePort

logger = logging.getLogger(__name__)


class MemoryJobCleanupObserver(Protocol):
    def cleanup_observed(self, result: MemoryJobPurgeResult) -> None:
        """Observe one successful bounded cleanup operation."""
        ...


class MemoryJobCleanupRunner:
    """Purge one bounded terminal batch immediately and once per interval."""

    def __init__(
        self,
        memory_job_queue: MemoryJobQueuePort,
        *,
        interval_seconds: float,
        completed_retention_seconds: float,
        dead_retention_seconds: float,
        batch_size: int,
        database_timeout_seconds: float,
        observer: MemoryJobCleanupObserver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_positive_finite(interval_seconds, "interval_seconds")
        _require_positive_finite(completed_retention_seconds, "completed_retention_seconds")
        _require_positive_finite(dead_retention_seconds, "dead_retention_seconds")
        _require_positive_integer(batch_size, "batch_size")
        _require_positive_finite(database_timeout_seconds, "database_timeout_seconds")
        self._queue = memory_job_queue
        self._interval = float(interval_seconds)
        self._completed_retention = float(completed_retention_seconds)
        self._dead_retention = float(dead_retention_seconds)
        self._batch_size = batch_size
        self._database_timeout = float(database_timeout_seconds)
        self._observer = observer
        self._clock = clock or _utc_now
        self._stop_requested = asyncio.Event()
        self._started = False
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def run(self) -> None:
        if self._started:
            raise RuntimeError("memory job cleanup runner is single-use")
        self._started = True
        self._running = True
        cancellation: asyncio.CancelledError | None = None
        try:
            while not self._stop_requested.is_set():
                await self._cleanup_once()
                await self._pause(self._interval)
        except asyncio.CancelledError as error:
            cancellation = error
        finally:
            self._stop_requested.set()
            self._running = False
        if cancellation is not None:
            raise cancellation

    async def _cleanup_once(self) -> None:
        try:
            now = self._clock()
            _require_aware_datetime(now)
            async with asyncio.timeout(self._database_timeout):
                result = await self._queue.purge_terminal(
                    completed_before=now - timedelta(seconds=self._completed_retention),
                    dead_before=now - timedelta(seconds=self._dead_retention),
                    limit=self._batch_size,
                )
            if not isinstance(result, MemoryJobPurgeResult):
                raise MemoryJobQueueProtocolError
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Memory job retention cleanup failed",
                extra={
                    "dependency": "postgresql",
                    "operation": "purge_terminal_memory_jobs",
                    "error_class": type(error).__name__,
                    "fallback_mode": "retry_next_cleanup_interval",
                },
            )
            return

        if self._observer is not None:
            try:
                self._observer.cleanup_observed(result)
            except Exception as error:
                logger.warning(
                    "Memory job cleanup observation failed",
                    extra={
                        "dependency": "prometheus",
                        "operation": "observe_memory_job_cleanup",
                        "error_class": type(error).__name__,
                        "fallback_mode": "continue_cleanup",
                    },
                )

    async def _pause(self, delay_seconds: float) -> None:
        try:
            async with asyncio.timeout(delay_seconds):
                await self._stop_requested.wait()
        except TimeoutError:
            return


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


def _require_aware_datetime(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")


def _utc_now() -> datetime:
    return datetime.now(UTC)
