"""Supervise the Worker runner and cache queue state for probes and metrics."""

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime

from app.domain.errors.memory_job import MemoryJobQueueProtocolError
from app.domain.models.memory_job import MemoryJobStats
from app.domain.ports.memory_job_queue import MemoryJobQueuePort
from worker.cleanup import MemoryJobCleanupRunner
from worker.runner import MemoryJobRunner
from worker.telemetry import MemoryJobTelemetry

logger = logging.getLogger(__name__)


class MemoryWorkerRuntime:
    """Own one runner task and one non-blocking queue-metrics sampler task."""

    def __init__(
        self,
        runner: MemoryJobRunner,
        memory_job_queue: MemoryJobQueuePort,
        telemetry: MemoryJobTelemetry,
        *,
        metrics_refresh_seconds: float,
        database_timeout_seconds: float,
        cleanup_runner: MemoryJobCleanupRunner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_positive_finite(metrics_refresh_seconds, "metrics_refresh_seconds")
        _require_positive_finite(database_timeout_seconds, "database_timeout_seconds")
        self._runner = runner
        self._queue = memory_job_queue
        self._telemetry = telemetry
        self._metrics_refresh = float(metrics_refresh_seconds)
        self._database_timeout = float(database_timeout_seconds)
        self._cleanup_runner = cleanup_runner
        self._freshness_window = (2 * self._metrics_refresh) + self._database_timeout
        self._clock = clock or _utc_now
        self._stop_requested = asyncio.Event()
        self._runner_task: asyncio.Task[None] | None = None
        self._metrics_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._last_queue_refresh_at: datetime | None = None
        self._queue_snapshot_available = False
        self._started = False
        self._stopped = False

    @property
    def is_ready(self) -> bool:
        """Require an active runner and a successful, fresh queue observation."""
        runner_task = self._runner_task
        snapshot = self._runner.snapshot
        return bool(
            self._started
            and not self._stopped
            and not self._stop_requested.is_set()
            and runner_task is not None
            and not runner_task.done()
            and snapshot.running
            and snapshot.database_available
            and self._queue_snapshot_is_fresh()
        )

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("memory Worker runtime is single-use")
        self._started = True
        self._runner_task = asyncio.create_task(self._run_runner(), name="memory-job-runner")
        self._metrics_task = asyncio.create_task(
            self._sample_queue_loop(),
            name="memory-job-metrics",
        )
        if self._cleanup_runner is not None:
            self._cleanup_task = asyncio.create_task(
                self._run_cleanup(),
                name="memory-job-cleanup",
            )
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if not self._started or self._stopped:
            return
        self._stop_requested.set()
        self._runner.request_stop()
        if self._cleanup_runner is not None:
            self._cleanup_runner.request_stop()
        tasks = tuple(
            task
            for task in (self._metrics_task, self._cleanup_task, self._runner_task)
            if task is not None
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stopped = True
        self.observe_runtime()

    def observe_runtime(self) -> None:
        """Refresh process gauges without querying PostgreSQL on the HTTP request path."""
        self._telemetry.runner_observed(
            self._runner.snapshot,
            queue_database_available=self._queue_snapshot_is_fresh(),
        )

    async def _run_runner(self) -> None:
        try:
            await self._runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Memory job runner stopped unexpectedly",
                extra={
                    "dependency": "memory_job_runtime",
                    "operation": "run_memory_jobs",
                    "error_class": type(error).__name__,
                    "fallback_mode": "not_ready",
                },
            )

    async def _sample_queue_loop(self) -> None:
        while not self._stop_requested.is_set():
            await self._sample_queue_once()
            await self._pause(self._metrics_refresh)

    async def _run_cleanup(self) -> None:
        if self._cleanup_runner is None:
            return
        try:
            await self._cleanup_runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Memory job cleanup runner stopped unexpectedly",
                extra={
                    "dependency": "memory_job_runtime",
                    "operation": "cleanup_memory_jobs",
                    "error_class": type(error).__name__,
                    "fallback_mode": "processing_continues",
                },
            )

    async def _sample_queue_once(self) -> None:
        try:
            async with asyncio.timeout(self._database_timeout):
                stats = await self._queue.stats()
            if not isinstance(stats, MemoryJobStats):
                raise MemoryJobQueueProtocolError
            now = self._clock()
            _require_aware_datetime(now)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._queue_snapshot_available = False
            logger.warning(
                "Memory job queue metrics refresh failed",
                extra={
                    "dependency": "postgresql",
                    "operation": "read_memory_job_stats",
                    "error_class": type(error).__name__,
                    "fallback_mode": "cached_metrics_not_ready",
                },
            )
            return

        self._telemetry.queue_stats_observed(stats)
        self._last_queue_refresh_at = now
        self._queue_snapshot_available = True

    def _queue_snapshot_is_fresh(self) -> bool:
        refreshed_at = self._last_queue_refresh_at
        if not self._queue_snapshot_available or refreshed_at is None:
            return False
        try:
            now = self._clock()
            _require_aware_datetime(now)
            age = (now - refreshed_at).total_seconds()
        except (TypeError, ValueError):
            return False
        return 0 <= age <= self._freshness_window

    async def _pause(self, delay_seconds: float) -> None:
        try:
            async with asyncio.timeout(delay_seconds):
                await self._stop_requested.wait()
        except TimeoutError:
            return


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
