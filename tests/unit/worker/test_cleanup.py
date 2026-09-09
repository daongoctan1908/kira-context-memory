import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.errors.memory_job import MemoryJobQueueConnectionError
from app.domain.models.memory_job import MemoryJobPurgeResult
from worker.cleanup import MemoryJobCleanupRunner

NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


class FakeQueue:
    def __init__(
        self,
        responses: tuple[object, ...] = (MemoryJobPurgeResult(),),
        *,
        block: bool = False,
    ) -> None:
        self.responses = deque(responses)
        self.block = block
        self.calls: list[dict[str, object]] = []
        self.failed = asyncio.Event()
        self.succeeded = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def purge_terminal(self, **kwargs) -> MemoryJobPurgeResult:
        self.calls.append(kwargs)
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        response = self.responses.popleft() if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, BaseException):
            self.failed.set()
            raise response
        self.succeeded.set()
        return response  # type: ignore[return-value]


class RecordingObserver:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.results: list[MemoryJobPurgeResult] = []
        self.observed = asyncio.Event()

    def cleanup_observed(self, result: MemoryJobPurgeResult) -> None:
        self.results.append(result)
        self.observed.set()
        if self.error is not None:
            raise self.error


def make_cleanup(
    queue: FakeQueue,
    *,
    observer: RecordingObserver | None = None,
    **overrides: object,
) -> MemoryJobCleanupRunner:
    values: dict[str, object] = {
        "interval_seconds": 0.01,
        "completed_retention_seconds": 7 * 24 * 60 * 60,
        "dead_retention_seconds": 30 * 24 * 60 * 60,
        "batch_size": 1000,
        "database_timeout_seconds": 0.02,
        "observer": observer,
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return MemoryJobCleanupRunner(
        queue,  # type: ignore[arg-type]
        **values,  # type: ignore[arg-type]
    )


async def test_cleanup_runs_immediately_with_exact_retention_cutoffs_and_batch() -> None:
    result = MemoryJobPurgeResult(completed=2, dead=1)
    queue = FakeQueue((result,))
    observer = RecordingObserver()
    cleanup = make_cleanup(queue, observer=observer, batch_size=25)

    task = asyncio.create_task(cleanup.run())
    await asyncio.wait_for(observer.observed.wait(), timeout=1)
    cleanup.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert queue.calls == [
        {
            "completed_before": NOW - timedelta(days=7),
            "dead_before": NOW - timedelta(days=30),
            "limit": 25,
        }
    ]
    assert observer.results == [result]
    assert cleanup.running is False


async def test_cleanup_database_failure_retries_only_on_next_interval(monkeypatch) -> None:
    queue = FakeQueue(
        (
            MemoryJobQueueConnectionError(),
            MemoryJobPurgeResult(completed=1),
        )
    )
    observer = RecordingObserver()
    cleanup = make_cleanup(queue, observer=observer)
    log_extras: list[dict[str, object]] = []

    monkeypatch.setattr(
        "worker.cleanup.logger.warning",
        lambda message, *, extra: log_extras.append(extra),
    )
    task = asyncio.create_task(cleanup.run())
    await asyncio.wait_for(queue.failed.wait(), timeout=1)
    assert observer.results == []
    await asyncio.wait_for(observer.observed.wait(), timeout=1)
    cleanup.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert len(queue.calls) >= 2
    assert observer.results[0] == MemoryJobPurgeResult(completed=1)
    assert log_extras == [
        {
            "dependency": "postgresql",
            "operation": "purge_terminal_memory_jobs",
            "error_class": "MemoryJobQueueConnectionError",
            "fallback_mode": "retry_next_cleanup_interval",
        }
    ]


async def test_cleanup_timeout_cancels_database_call_and_stops_cleanly(monkeypatch) -> None:
    queue = FakeQueue(block=True)
    cleanup = make_cleanup(queue, database_timeout_seconds=0.005, interval_seconds=10)
    log_extras: list[dict[str, object]] = []

    monkeypatch.setattr(
        "worker.cleanup.logger.warning",
        lambda message, *, extra: log_extras.append(extra),
    )
    task = asyncio.create_task(cleanup.run())
    await asyncio.wait_for(queue.cancelled.wait(), timeout=1)
    cleanup.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert log_extras[0]["error_class"] == "TimeoutError"
    assert cleanup.running is False


async def test_protocol_and_observer_failures_are_isolated_and_sanitized(monkeypatch) -> None:
    queue = FakeQueue(("private invalid result",))
    cleanup = make_cleanup(queue, interval_seconds=0.01)
    log_extras: list[dict[str, object]] = []
    logged = asyncio.Event()

    def capture_warning(message: str, *, extra: dict[str, object]) -> None:
        log_extras.append(extra)
        logged.set()

    monkeypatch.setattr("worker.cleanup.logger.warning", capture_warning)
    task = asyncio.create_task(cleanup.run())
    await asyncio.wait_for(logged.wait(), timeout=1)
    cleanup.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert log_extras[0]["error_class"] == "MemoryJobQueueProtocolError"
    assert "private invalid result" not in repr(log_extras)

    observer = RecordingObserver(error=RuntimeError("private metrics detail"))
    observed_cleanup = make_cleanup(FakeQueue(), observer=observer, interval_seconds=10)
    task = asyncio.create_task(observed_cleanup.run())
    await asyncio.wait_for(observer.observed.wait(), timeout=1)
    observed_cleanup.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert log_extras[-1]["error_class"] == "RuntimeError"
    assert "private metrics detail" not in repr(log_extras)


async def test_external_cancellation_propagates_without_converting_to_cleanup_failure() -> None:
    queue = FakeQueue(block=True)
    cleanup = make_cleanup(queue, database_timeout_seconds=10)
    task = asyncio.create_task(cleanup.run())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.cancelled.is_set()
    assert cleanup.running is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interval_seconds", 0),
        ("completed_retention_seconds", -1),
        ("dead_retention_seconds", float("inf")),
        ("batch_size", 0),
        ("batch_size", True),
        ("database_timeout_seconds", 0),
    ],
)
def test_cleanup_rejects_invalid_runtime_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        make_cleanup(FakeQueue(), **{field: value})


async def test_cleanup_runner_is_single_use() -> None:
    cleanup = make_cleanup(FakeQueue())
    cleanup.request_stop()

    await cleanup.run()
    with pytest.raises(RuntimeError, match="single-use"):
        await cleanup.run()
