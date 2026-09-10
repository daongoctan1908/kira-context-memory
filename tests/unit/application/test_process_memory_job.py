import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.application.use_cases.process_memory_job import (
    MemoryJobProcessOutcome,
    ProcessMemoryJobUseCase,
)
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
from app.domain.errors.memory_job import MemoryJobLeaseLostError
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.memory import MemoryLifecycleEvent, MemoryProcessResult, MemorySource
from app.domain.models.memory_job import MemoryJob

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
RETRY_DELAYS = (1.0, 5.0, 30.0, 120.0)


def make_job(*, attempt_count: int = 1) -> MemoryJob:
    return MemoryJob(
        event_id=uuid4(),
        reference=CompletedTurnReference(
            user_id="worker-user",
            session_id="worker-session",
            conversation_id=uuid4(),
            turn_id="worker-turn",
            boundary_message_id=42,
        ),
        attempt_count=attempt_count,
        lease_token=uuid4(),
        lease_expires_at=NOW + timedelta(minutes=2),
    )


class FakeMemoryProcessor:
    def __init__(
        self,
        *,
        result: MemoryProcessResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or MemoryProcessResult()
        self.error = error
        self.calls: list[tuple[CompletedTurnReference, object]] = []

    async def execute(
        self,
        reference: CompletedTurnReference,
        formation_event_id: object,
    ) -> MemoryProcessResult:
        self.calls.append((reference, formation_event_id))
        if self.error is not None:
            raise self.error
        return self.result


class FakeQueue:
    def __init__(self, *, transition_error: BaseException | None = None) -> None:
        self.transition_error = transition_error
        self.completed: list[tuple[object, object, int]] = []
        self.retried: list[tuple[object, object, datetime, str]] = []
        self.dead: list[tuple[object, object, str]] = []

    async def complete(
        self,
        event_id: object,
        lease_token: object,
        *,
        lifecycle_event_count: int,
    ) -> None:
        if self.transition_error is not None:
            raise self.transition_error
        self.completed.append((event_id, lease_token, lifecycle_event_count))

    async def retry(
        self,
        event_id: object,
        lease_token: object,
        *,
        next_attempt_at: datetime,
        error_class: str,
    ) -> None:
        if self.transition_error is not None:
            raise self.transition_error
        self.retried.append((event_id, lease_token, next_attempt_at, error_class))

    async def dead_letter(
        self,
        event_id: object,
        lease_token: object,
        *,
        error_class: str,
    ) -> None:
        if self.transition_error is not None:
            raise self.transition_error
        self.dead.append((event_id, lease_token, error_class))


class ExactBoundaryStore:
    def __init__(self, messages: tuple[ConversationMessage, ...]) -> None:
        self.messages = messages
        self.calls: list[tuple[object, ...]] = []

    async def read_through_boundary(
        self,
        user_id: str,
        conversation_id: object,
        boundary_message_id: int,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        self.calls.append((user_id, conversation_id, boundary_message_id, limit))
        return self.messages


class FormationMemory:
    def __init__(self, result: MemoryProcessResult) -> None:
        self.result = result
        self.sources: list[MemorySource] = []

    async def process_memory(self, source: MemorySource) -> MemoryProcessResult:
        self.sources.append(source)
        return self.result


def make_use_case(
    processor: FakeMemoryProcessor,
    queue: FakeQueue,
    *,
    clock=lambda: NOW,
) -> ProcessMemoryJobUseCase:
    return ProcessMemoryJobUseCase(
        processor,
        queue,  # type: ignore[arg-type]
        max_attempts=5,
        retry_delays_seconds=RETRY_DELAYS,
        clock=clock,
    )


async def test_success_completes_exact_job_with_native_lifecycle_event_count() -> None:
    job = make_job()
    provider_result = MemoryProcessResult(
        (
            MemoryLifecycleEvent("ADD", "memory-1", "fact"),
            MemoryLifecycleEvent("UPDATE", "memory-2", "correction"),
            MemoryLifecycleEvent("DELETE", "memory-3"),
            MemoryLifecycleEvent("NONE"),
        )
    )
    processor = FakeMemoryProcessor(result=provider_result)
    queue = FakeQueue()

    result = await make_use_case(processor, queue).execute(job)

    assert result.outcome is MemoryJobProcessOutcome.COMPLETED
    assert result.lifecycle_event_count == 4
    assert result.error_class is None
    assert result.next_attempt_at is None
    assert processor.calls == [(job.reference, job.event_id)]
    assert queue.completed == [(job.event_id, job.lease_token, 4)]
    assert queue.retried == []
    assert queue.dead == []


async def test_job_runs_through_exact_boundary_process_memory_use_case() -> None:
    job = make_job()
    messages = (
        ConversationMessage(
            job.reference.session_id,
            job.reference.turn_id,
            ConversationRole.USER,
            "remember this durable fact",
            NOW,
        ),
        ConversationMessage(
            job.reference.session_id,
            job.reference.turn_id,
            ConversationRole.ASSISTANT,
            "confirmed",
            NOW + timedelta(milliseconds=1),
        ),
    )
    store = ExactBoundaryStore(messages)
    memory_result = MemoryProcessResult((MemoryLifecycleEvent("ADD", "memory-1", "fact"),))
    memory = FormationMemory(memory_result)
    processor = ProcessMemoryUseCase(
        store,  # type: ignore[arg-type]
        memory,  # type: ignore[arg-type]
        message_limit=2,
    )
    queue = FakeQueue()
    use_case = ProcessMemoryJobUseCase(
        processor,
        queue,  # type: ignore[arg-type]
        max_attempts=5,
        retry_delays_seconds=RETRY_DELAYS,
        clock=lambda: NOW,
    )

    result = await use_case.execute(job)

    assert result.outcome is MemoryJobProcessOutcome.COMPLETED
    assert store.calls == [
        (
            job.reference.user_id,
            job.reference.conversation_id,
            job.reference.boundary_message_id,
            2,
        )
    ]
    assert memory.sources[0].reference is job.reference
    assert memory.sources[0].messages == messages
    assert memory.sources[0].formation_event_id == job.event_id
    assert queue.completed == [(job.event_id, job.lease_token, 1)]


@pytest.mark.parametrize(
    "error",
    [
        ConversationStoreConnectionError(),
        ConversationStoreOperationError(),
        LongTermMemoryConnectionError(),
        LongTermMemoryOperationError(),
        LongTermMemoryTimeoutError(),
    ],
)
async def test_retryable_dependency_errors_schedule_the_matching_attempt_delay(
    error: Exception,
) -> None:
    job = make_job(attempt_count=2)
    queue = FakeQueue()

    result = await make_use_case(FakeMemoryProcessor(error=error), queue).execute(job)

    assert result.outcome is MemoryJobProcessOutcome.RETRY
    assert result.lifecycle_event_count == 0
    assert result.error_class == type(error).__name__
    assert result.next_attempt_at == NOW + timedelta(seconds=5)
    assert queue.retried == [
        (
            job.event_id,
            job.lease_token,
            NOW + timedelta(seconds=5),
            type(error).__name__,
        )
    ]
    assert queue.completed == []
    assert queue.dead == []


@pytest.mark.parametrize(
    ("attempt_count", "expected_delay"),
    [(1, 1), (2, 5), (3, 30), (4, 120)],
)
async def test_retry_backoff_is_indexed_by_current_delivery_attempt(
    attempt_count: int,
    expected_delay: int,
) -> None:
    job = make_job(attempt_count=attempt_count)
    queue = FakeQueue()

    await make_use_case(
        FakeMemoryProcessor(error=LongTermMemoryTimeoutError()),
        queue,
    ).execute(job)

    assert queue.retried[0][2] == NOW + timedelta(seconds=expected_delay)


async def test_retryable_error_on_fifth_attempt_is_dead_lettered_without_sixth_call() -> None:
    job = make_job(attempt_count=5)
    queue = FakeQueue()

    result = await make_use_case(
        FakeMemoryProcessor(error=LongTermMemoryTimeoutError()),
        queue,
    ).execute(job)

    assert result.outcome is MemoryJobProcessOutcome.DEAD
    assert result.error_class == "LongTermMemoryTimeoutError"
    assert result.next_attempt_at is None
    assert queue.dead == [(job.event_id, job.lease_token, "LongTermMemoryTimeoutError")]
    assert queue.retried == []


@pytest.mark.parametrize(
    "error",
    [
        ConversationStoreConfigurationError(),
        ConversationStoreProtocolError(),
        LongTermMemoryConfigurationError(),
        LongTermMemoryProtocolError(),
    ],
)
async def test_permanent_boundary_configuration_and_protocol_errors_go_directly_dead(
    error: Exception,
) -> None:
    job = make_job()
    queue = FakeQueue()

    result = await make_use_case(FakeMemoryProcessor(error=error), queue).execute(job)

    assert result.outcome is MemoryJobProcessOutcome.DEAD
    assert result.error_class == type(error).__name__
    assert queue.dead == [(job.event_id, job.lease_token, type(error).__name__)]
    assert queue.retried == []


async def test_unexpected_runtime_error_is_retried_with_only_its_class_name() -> None:
    job = make_job()
    queue = FakeQueue()

    result = await make_use_case(
        FakeMemoryProcessor(error=RuntimeError("private provider detail")),
        queue,
    ).execute(job)

    assert queue.completed == []
    assert queue.retried == [
        (job.event_id, job.lease_token, NOW + timedelta(seconds=1), "RuntimeError")
    ]
    assert queue.dead == []
    assert result.outcome is MemoryJobProcessOutcome.RETRY
    assert result.error_class == "RuntimeError"
    assert "private provider detail" not in repr(result)


async def test_unexpected_runtime_error_is_dead_on_final_attempt() -> None:
    job = make_job(attempt_count=5)
    queue = FakeQueue()

    result = await make_use_case(
        FakeMemoryProcessor(error=RuntimeError("private provider detail")),
        queue,
    ).execute(job)

    assert result.outcome is MemoryJobProcessOutcome.DEAD
    assert queue.dead == [(job.event_id, job.lease_token, "RuntimeError")]


async def test_cancellation_propagates_and_leaves_job_under_its_lease() -> None:
    queue = FakeQueue()

    with pytest.raises(asyncio.CancelledError):
        await make_use_case(
            FakeMemoryProcessor(error=asyncio.CancelledError()),
            queue,
        ).execute(make_job())

    assert queue.completed == []
    assert queue.retried == []
    assert queue.dead == []


@pytest.mark.parametrize(
    "provider_error",
    [None, LongTermMemoryTimeoutError(), LongTermMemoryProtocolError()],
)
async def test_queue_transition_failure_propagates_without_a_second_transition(
    provider_error: Exception | None,
) -> None:
    queue_error = MemoryJobLeaseLostError()
    queue = FakeQueue(transition_error=queue_error)

    with pytest.raises(MemoryJobLeaseLostError):
        await make_use_case(
            FakeMemoryProcessor(error=provider_error),
            queue,
        ).execute(make_job())

    assert queue.completed == []
    assert queue.retried == []
    assert queue.dead == []


def test_constructor_rejects_invalid_attempt_and_retry_contract() -> None:
    processor = FakeMemoryProcessor()
    queue = FakeQueue()

    with pytest.raises(ValueError, match="max_attempts"):
        ProcessMemoryJobUseCase(
            processor,
            queue,  # type: ignore[arg-type]
            max_attempts=0,
            retry_delays_seconds=(),
        )
    with pytest.raises(ValueError, match="retry delays"):
        ProcessMemoryJobUseCase(
            processor,
            queue,  # type: ignore[arg-type]
            max_attempts=3,
            retry_delays_seconds=(1, 0),
        )


async def test_execute_rejects_invalid_job_or_attempt_without_touching_dependencies() -> None:
    processor = FakeMemoryProcessor()
    queue = FakeQueue()
    use_case = make_use_case(processor, queue)

    with pytest.raises(TypeError, match="MemoryJob"):
        await use_case.execute(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="attempt exceeds"):
        await use_case.execute(make_job(attempt_count=6))

    assert processor.calls == []


async def test_retry_requires_timezone_aware_clock() -> None:
    use_case = make_use_case(
        FakeMemoryProcessor(error=LongTermMemoryTimeoutError()),
        FakeQueue(),
        clock=lambda: datetime(2026, 9, 9, 12, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await use_case.execute(make_job())
