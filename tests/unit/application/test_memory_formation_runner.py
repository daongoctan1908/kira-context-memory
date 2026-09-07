import asyncio

from prometheus_client import generate_latest

from app.application.services.memory_formation_runner import MemoryFormationRunner
from app.domain.errors.conversation import ConversationStoreConnectionError
from app.domain.errors.memory import LongTermMemoryTimeoutError
from app.domain.models.memory import MemoryLifecycleEvent, MemoryProcessResult
from app.infrastructure.observability.context import ContextTelemetry
from tests.unit.application.test_process_memory import reference


class FakeProcessMemory:
    def __init__(self, result=None, error=None):
        self.result = result or MemoryProcessResult()
        self.error = error
        self.references = []

    async def execute(self, value):
        self.references.append(value)
        if self.error:
            raise self.error
        return self.result


async def test_runner_records_processed_events_without_lifecycle_action_labels():
    telemetry = ContextTelemetry()
    ref = reference()
    use_case = FakeProcessMemory(
        MemoryProcessResult(
            (
                MemoryLifecycleEvent("ADD", "memory-1"),
                MemoryLifecycleEvent("PROVIDER_FUTURE_ACTION", "memory-2"),
            )
        )
    )
    runner = MemoryFormationRunner(use_case, telemetry, shutdown_timeout_seconds=1)

    runner.submit(ref, "correlation-1")
    await runner.wait_idle()

    payload = generate_latest(telemetry.registry).decode()
    assert use_case.references == [ref]
    assert 'kira_memory_formation_total{outcome="processed"} 1.0' in payload
    assert "kira_memory_formation_events_sum 2.0" in payload
    assert "PROVIDER_FUTURE_ACTION" not in payload
    assert "correlation-1" not in payload


async def test_runner_records_no_change():
    telemetry = ContextTelemetry()
    runner = MemoryFormationRunner(FakeProcessMemory(), telemetry, shutdown_timeout_seconds=1)
    runner.submit(reference(), "correlation-1")
    await runner.wait_idle()

    assert (
        telemetry.registry.get_sample_value("kira_memory_formation_total", {"outcome": "no_change"})
        == 1
    )


async def test_runner_maps_source_and_mem0_failures_without_leaking_task_exceptions():
    for error, dependency, operation in (
        (ConversationStoreConnectionError(), "postgresql", "memory_source_read"),
        (LongTermMemoryTimeoutError(), "mem0", "memory_formation"),
        (RuntimeError("private message"), "mem0", "memory_formation"),
    ):
        telemetry = ContextTelemetry()
        runner = MemoryFormationRunner(
            FakeProcessMemory(error=error), telemetry, shutdown_timeout_seconds=1
        )
        runner.submit(reference(), "private-correlation")
        await runner.wait_idle()

        assert (
            telemetry.registry.get_sample_value(
                "kira_context_degraded_total",
                {"dependency": dependency, "operation": operation},
            )
            == 1
        )


async def test_close_cancels_work_that_exceeds_shutdown_deadline():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingProcessMemory:
        async def execute(self, value):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    telemetry = ContextTelemetry()
    runner = MemoryFormationRunner(HangingProcessMemory(), telemetry, shutdown_timeout_seconds=0.01)
    runner.submit(reference(), "correlation-1")
    await started.wait()
    await runner.aclose()

    assert cancelled.is_set()
    assert (
        telemetry.registry.get_sample_value("kira_memory_formation_total", {"outcome": "cancelled"})
        == 1
    )


async def test_closed_runner_rejects_new_work():
    runner = MemoryFormationRunner(
        FakeProcessMemory(), ContextTelemetry(), shutdown_timeout_seconds=1
    )
    await runner.aclose()

    try:
        runner.submit(reference(), "correlation-1")
    except RuntimeError as error:
        assert str(error) == "memory formation runner is closed"
    else:
        raise AssertionError("closed runner accepted work")
