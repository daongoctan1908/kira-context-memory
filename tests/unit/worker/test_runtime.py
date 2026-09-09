import asyncio
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.errors.memory_job import MemoryJobQueueConnectionError
from app.domain.models.memory_job import MemoryJobStats
from worker.runner import MemoryJobRunnerSnapshot
from worker.runtime import MemoryWorkerRuntime
from worker.telemetry import MemoryJobTelemetry

NOW = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)


class FakeRunner:
    def __init__(self, *, database_available: bool = True, error: Exception | None = None) -> None:
        self._snapshot = MemoryJobRunnerSnapshot(False, False, False, None, 0, 0.0, 0)
        self.database_available = database_available
        self.error = error
        self.started = asyncio.Event()
        self.stop_requested = asyncio.Event()

    @property
    def snapshot(self) -> MemoryJobRunnerSnapshot:
        return self._snapshot

    async def run(self) -> None:
        self._snapshot = replace(
            self._snapshot,
            running=True,
            database_available=self.database_available,
            last_successful_poll_at=NOW if self.database_available else None,
        )
        self.started.set()
        try:
            if self.error is not None:
                raise self.error
            await self.stop_requested.wait()
        finally:
            self._snapshot = replace(self._snapshot, running=False, stopping=True)

    def request_stop(self) -> None:
        self.stop_requested.set()


class FakeStatsQueue:
    def __init__(
        self,
        responses: tuple[object, ...] = (MemoryJobStats(1, 2, 3, 4, 5.0),),
        *,
        block: bool = False,
        hold_second_call: bool = False,
    ) -> None:
        self.responses = deque(responses)
        self.block = block
        self.hold_second_call = hold_second_call
        self.release_second_call = asyncio.Event()
        self.calls = 0
        self.called = asyncio.Event()
        self.failed = asyncio.Event()
        self.succeeded = asyncio.Event()
        self.cancelled = False
        self.cancelled_event = asyncio.Event()

    async def stats(self) -> MemoryJobStats:
        self.calls += 1
        self.called.set()
        if self.hold_second_call and self.calls == 2:
            await self.release_second_call.wait()
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                self.cancelled_event.set()
                raise
        response = self.responses.popleft() if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, BaseException):
            self.failed.set()
            raise response
        self.succeeded.set()
        return response  # type: ignore[return-value]


def make_runtime(
    runner: FakeRunner,
    queue: FakeStatsQueue,
    **overrides: object,
) -> MemoryWorkerRuntime:
    values: dict[str, object] = {
        "metrics_refresh_seconds": 0.01,
        "database_timeout_seconds": 0.02,
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return MemoryWorkerRuntime(
        runner,  # type: ignore[arg-type]
        queue,  # type: ignore[arg-type]
        MemoryJobTelemetry(),
        **values,  # type: ignore[arg-type]
    )


async def test_runtime_is_ready_only_with_active_runner_and_fresh_queue_snapshot() -> None:
    runner = FakeRunner()
    queue = FakeStatsQueue()
    runtime = make_runtime(runner, queue)

    assert runtime.is_ready is False
    await runtime.start()
    await asyncio.wait_for(runner.started.wait(), timeout=1)
    await asyncio.wait_for(queue.succeeded.wait(), timeout=1)
    await asyncio.sleep(0)
    runtime.observe_runtime()

    assert runtime.is_ready is True
    await runtime.stop()
    assert runtime.is_ready is False
    assert runner.stop_requested.is_set()


async def test_queue_failure_marks_not_ready_then_success_recovers() -> None:
    runner = FakeRunner()
    queue = FakeStatsQueue(
        (
            MemoryJobQueueConnectionError(),
            MemoryJobStats(0, 0, 0, 0),
        ),
        hold_second_call=True,
    )
    runtime = make_runtime(runner, queue)

    await runtime.start()
    await asyncio.wait_for(queue.failed.wait(), timeout=1)
    await asyncio.sleep(0)
    assert runtime.is_ready is False
    queue.release_second_call.set()
    await asyncio.wait_for(queue.succeeded.wait(), timeout=1)
    await asyncio.sleep(0)
    assert runtime.is_ready is True
    assert queue.calls >= 2
    await runtime.stop()


async def test_queue_stats_timeout_is_cancelled_and_keeps_runtime_not_ready() -> None:
    runner = FakeRunner()
    queue = FakeStatsQueue(block=True)
    runtime = make_runtime(runner, queue, database_timeout_seconds=0.005)

    await runtime.start()
    await asyncio.wait_for(queue.cancelled_event.wait(), timeout=1)

    assert runtime.is_ready is False
    await runtime.stop()


async def test_stale_queue_snapshot_fails_readiness_without_http_database_query() -> None:
    current = [NOW]
    runner = FakeRunner()
    queue = FakeStatsQueue()
    runtime = make_runtime(
        runner,
        queue,
        metrics_refresh_seconds=10,
        database_timeout_seconds=1,
        clock=lambda: current[0],
    )

    await runtime.start()
    await asyncio.wait_for(runner.started.wait(), timeout=1)
    await asyncio.wait_for(queue.succeeded.wait(), timeout=1)
    await asyncio.sleep(0)
    calls_before_probe = queue.calls
    current[0] = NOW + timedelta(seconds=22)

    assert runtime.is_ready is False
    runtime.observe_runtime()
    assert queue.calls == calls_before_probe
    await runtime.stop()


async def test_stopped_runner_makes_runtime_not_ready_and_logs_safely(monkeypatch) -> None:
    runner = FakeRunner(error=RuntimeError("private runner detail"))
    queue = FakeStatsQueue()
    runtime = make_runtime(runner, queue)
    log_extras: list[dict[str, object]] = []
    logged = asyncio.Event()

    def capture_error(message: str, *, extra: dict[str, object]) -> None:
        log_extras.append(extra)
        logged.set()

    monkeypatch.setattr("worker.runtime.logger.error", capture_error)
    await runtime.start()
    await asyncio.wait_for(logged.wait(), timeout=1)

    assert runtime.is_ready is False
    assert log_extras == [
        {
            "dependency": "memory_job_runtime",
            "operation": "run_memory_jobs",
            "error_class": "RuntimeError",
            "fallback_mode": "not_ready",
        }
    ]
    assert "private runner detail" not in repr(log_extras)
    await runtime.stop()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metrics_refresh_seconds", 0),
        ("metrics_refresh_seconds", float("inf")),
        ("database_timeout_seconds", -1),
        ("database_timeout_seconds", True),
    ],
)
def test_runtime_rejects_invalid_time_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        make_runtime(FakeRunner(), FakeStatsQueue(), **{field: value})
