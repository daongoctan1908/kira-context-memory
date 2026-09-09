import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobResult,
)
from app.domain.errors.memory_job import (
    MemoryJobLeaseLostError,
    MemoryJobQueueConnectionError,
    MemoryJobQueueOperationError,
)
from app.domain.models.conversation import CompletedTurnReference
from app.domain.models.memory_job import MemoryJob
from worker.runner import MemoryJobRunner, _database_backoff_seconds

NOW = datetime(2026, 9, 9, 13, 0, tzinfo=UTC)


def make_job(*, reclaimed: bool = False, attempt_count: int = 1) -> MemoryJob:
    return MemoryJob(
        event_id=uuid4(),
        reference=CompletedTurnReference(
            user_id="runner-user",
            session_id="runner-session",
            conversation_id=uuid4(),
            turn_id=f"runner-turn-{uuid4()}",
            boundary_message_id=42,
        ),
        attempt_count=attempt_count,
        lease_token=uuid4(),
        lease_expires_at=NOW + timedelta(minutes=2),
        reclaimed=reclaimed,
    )


class FakeClaimQueue:
    def __init__(
        self,
        jobs: tuple[MemoryJob, ...] = (),
        *,
        responses: tuple[object, ...] = (),
        block_claim: bool = False,
    ) -> None:
        self.jobs = deque(jobs)
        self.responses = deque(responses)
        self.block_claim = block_claim
        self.claim_calls: list[dict[str, object]] = []
        self.successful_poll = asyncio.Event()
        self.claim_cancelled = False
        self.claim_cancelled_event = asyncio.Event()

    async def claim_due(
        self,
        *,
        lease_owner: UUID,
        limit: int,
        lease_seconds: float,
        max_attempts: int,
    ) -> tuple[MemoryJob, ...]:
        self.claim_calls.append(
            {
                "lease_owner": lease_owner,
                "limit": limit,
                "lease_seconds": lease_seconds,
                "max_attempts": max_attempts,
            }
        )
        if self.block_claim:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.claim_cancelled = True
                self.claim_cancelled_event.set()
                raise
        if self.responses:
            response = self.responses.popleft()
            if isinstance(response, BaseException):
                raise response
            self.successful_poll.set()
            return response  # type: ignore[return-value]
        claimed = tuple(self.jobs.popleft() for _ in range(min(limit, len(self.jobs))))
        self.successful_poll.set()
        return claimed


class BlockingProcessor:
    def __init__(
        self,
        *,
        target_started: int = 1,
        release_immediately: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.target_started = target_started
        self.error = error
        self.release = asyncio.Event()
        if release_immediately:
            self.release.set()
        self.target_reached = asyncio.Event()
        self.finished = asyncio.Event()
        self.jobs: list[MemoryJob] = []
        self.current = 0
        self.max_concurrency = 0
        self.cancelled = 0

    async def execute(self, job: MemoryJob) -> ProcessMemoryJobResult:
        self.jobs.append(job)
        self.current += 1
        self.max_concurrency = max(self.max_concurrency, self.current)
        if len(self.jobs) >= self.target_started:
            self.target_reached.set()
        try:
            if self.error is not None:
                raise self.error
            await self.release.wait()
            return ProcessMemoryJobResult(MemoryJobProcessOutcome.COMPLETED)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.current -= 1
            self.finished.set()


def make_runner(
    queue: FakeClaimQueue,
    processor: BlockingProcessor,
    **overrides: object,
) -> MemoryJobRunner:
    values: dict[str, object] = {
        "poll_interval_seconds": 0.005,
        "batch_size": 10,
        "concurrency": 4,
        "lease_seconds": 120,
        "max_attempts": 5,
        "database_timeout_seconds": 0.05,
        "shutdown_grace_seconds": 0.1,
        "lease_owner": UUID("00000000-0000-0000-0000-000000000123"),
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return MemoryJobRunner(
        queue,  # type: ignore[arg-type]
        processor,
        **values,  # type: ignore[arg-type]
    )


async def test_runner_claims_only_free_slots_and_honors_bounded_concurrency() -> None:
    jobs = tuple(make_job() for _ in range(6))
    queue = FakeClaimQueue(jobs)
    processor = BlockingProcessor(target_started=4)
    runner = make_runner(queue, processor, batch_size=10, concurrency=4)

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(processor.target_reached.wait(), timeout=1)

    assert processor.max_concurrency == 4
    assert len(processor.jobs) == 4
    assert len(queue.jobs) == 2
    assert [call["limit"] for call in queue.claim_calls] == [4]
    assert runner.snapshot.in_flight_count == 4

    runner.request_stop()
    processor.release.set()
    await asyncio.wait_for(run_task, timeout=1)

    assert len(processor.jobs) == 4
    assert processor.cancelled == 0
    assert runner.snapshot.running is False


async def test_runner_repeats_batch_limited_claims_until_capacity_is_full() -> None:
    jobs = tuple(make_job() for _ in range(5))
    queue = FakeClaimQueue(jobs)
    processor = BlockingProcessor(target_started=5)
    runner = make_runner(queue, processor, batch_size=2, concurrency=5)

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(processor.target_reached.wait(), timeout=1)

    assert [call["limit"] for call in queue.claim_calls] == [2, 2, 1]
    assert processor.max_concurrency == 5
    runner.request_stop()
    processor.release.set()
    await asyncio.wait_for(run_task, timeout=1)


async def test_runner_passes_lease_contract_and_processes_reclaimed_job() -> None:
    lease_owner = uuid4()
    job = make_job(reclaimed=True, attempt_count=2)
    queue = FakeClaimQueue((job,))
    processor = BlockingProcessor(release_immediately=True)
    runner = make_runner(
        queue,
        processor,
        lease_owner=lease_owner,
        lease_seconds=77,
        max_attempts=5,
    )

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(processor.finished.wait(), timeout=1)
    runner.request_stop()
    await asyncio.wait_for(run_task, timeout=1)

    assert processor.jobs == [job]
    assert processor.jobs[0].reclaimed is True
    assert queue.claim_calls[0] == {
        "lease_owner": lease_owner,
        "limit": 4,
        "lease_seconds": 77.0,
        "max_attempts": 5,
    }


async def test_database_errors_back_off_then_success_resets_readiness_state() -> None:
    queue = FakeClaimQueue(
        responses=(
            MemoryJobQueueConnectionError(),
            MemoryJobQueueOperationError(),
            (),
        )
    )
    processor = BlockingProcessor(release_immediately=True)
    runner = make_runner(queue, processor, poll_interval_seconds=0.001)

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(queue.successful_poll.wait(), timeout=1)

    snapshot = runner.snapshot
    assert len(queue.claim_calls) >= 3
    assert snapshot.database_available is True
    assert snapshot.last_successful_poll_at == NOW
    assert snapshot.consecutive_database_failures == 0
    assert snapshot.database_backoff_seconds == 0

    runner.request_stop()
    await asyncio.wait_for(run_task, timeout=1)


async def test_database_timeout_cancels_claim_and_enters_backoff() -> None:
    queue = FakeClaimQueue(block_claim=True)
    processor = BlockingProcessor()
    runner = make_runner(
        queue,
        processor,
        database_timeout_seconds=0.01,
        poll_interval_seconds=0.05,
    )

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(queue.claim_cancelled_event.wait(), timeout=1)
    await asyncio.sleep(0)

    snapshot = runner.snapshot
    assert queue.claim_cancelled is True
    assert snapshot.database_available is False
    assert snapshot.consecutive_database_failures == 1
    assert snapshot.database_backoff_seconds == 0.05

    runner.request_stop()
    await asyncio.wait_for(run_task, timeout=1)


async def test_graceful_stop_drains_in_flight_job_without_cancelling_it() -> None:
    queue = FakeClaimQueue((make_job(),))
    processor = BlockingProcessor()
    runner = make_runner(queue, processor, shutdown_grace_seconds=0.2)

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(processor.target_reached.wait(), timeout=1)
    runner.request_stop()
    processor.release.set()
    await asyncio.wait_for(run_task, timeout=1)

    assert processor.cancelled == 0
    assert runner.snapshot.running is False
    assert runner.snapshot.stopping is True
    assert runner.snapshot.in_flight_count == 0


async def test_shutdown_grace_expiry_cancels_job_for_future_lease_reclaim() -> None:
    queue = FakeClaimQueue((make_job(),))
    processor = BlockingProcessor()
    runner = make_runner(queue, processor, shutdown_grace_seconds=0.01)

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(processor.target_reached.wait(), timeout=1)
    runner.request_stop()
    await asyncio.wait_for(run_task, timeout=1)

    assert processor.cancelled == 1
    assert runner.snapshot.running is False
    assert runner.snapshot.in_flight_count == 0


async def test_external_cancellation_drains_then_propagates_cancellation() -> None:
    queue = FakeClaimQueue((make_job(),))
    processor = BlockingProcessor()
    runner = make_runner(queue, processor, shutdown_grace_seconds=0.2)

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(processor.target_reached.wait(), timeout=1)
    run_task.cancel()
    processor.release.set()

    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert processor.cancelled == 0
    assert runner.snapshot.running is False


async def test_per_job_transition_failure_is_isolated_for_lease_reclaim(monkeypatch) -> None:
    private_job = make_job()
    queue = FakeClaimQueue((private_job,))
    processor = BlockingProcessor(
        release_immediately=True,
        error=MemoryJobLeaseLostError(),
    )
    runner = make_runner(queue, processor)
    logged = asyncio.Event()
    log_extras: list[dict[str, object]] = []

    def capture_warning(message: str, *, extra: dict[str, object]) -> None:
        log_extras.append(extra)
        logged.set()

    monkeypatch.setattr("worker.runner.logger.warning", capture_warning)

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(processor.finished.wait(), timeout=1)
    await asyncio.wait_for(logged.wait(), timeout=1)

    assert run_task.done() is False
    assert "runner-user" not in repr(log_extras)
    assert str(private_job.event_id) not in repr(log_extras)
    assert log_extras[-1]["error_class"] == "MemoryJobLeaseLostError"

    runner.request_stop()
    await asyncio.wait_for(run_task, timeout=1)


@pytest.mark.parametrize(
    ("failures", "poll_interval", "expected"),
    [
        (0, 1, 0),
        (1, 1, 1),
        (2, 1, 2),
        (3, 1, 4),
        (6, 1, 30),
        (100, 1, 30),
        (2, 40, 40),
    ],
)
def test_database_backoff_is_exponential_and_bounded(
    failures: int,
    poll_interval: float,
    expected: float,
) -> None:
    assert _database_backoff_seconds(failures, poll_interval) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_interval_seconds", 0),
        ("batch_size", 0),
        ("concurrency", 0),
        ("lease_seconds", float("inf")),
        ("max_attempts", True),
        ("database_timeout_seconds", -1),
        ("shutdown_grace_seconds", 0),
        ("lease_owner", "not-a-uuid"),
    ],
)
def test_runner_rejects_invalid_runtime_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        make_runner(FakeClaimQueue(), BlockingProcessor(), **{field: value})


async def test_runner_is_single_use() -> None:
    runner = make_runner(FakeClaimQueue(), BlockingProcessor(release_immediately=True))
    runner.request_stop()

    await runner.run()
    with pytest.raises(RuntimeError, match="single-use"):
        await runner.run()
