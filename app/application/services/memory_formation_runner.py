"""Managed in-process execution for best-effort memory formation."""

import asyncio
from time import perf_counter

from app.application.use_cases.process_memory import ProcessMemoryUseCase
from app.domain.errors.conversation import ConversationStoreError
from app.domain.errors.memory import LongTermMemoryError
from app.domain.models.conversation import CompletedTurnReference
from app.domain.ports.context_observer import ContextObserverPort


class MemoryFormationRunner:
    """Run formation off the response path without pretending to be a durable queue."""

    def __init__(
        self,
        use_case: ProcessMemoryUseCase,
        observer: ContextObserverPort,
        *,
        shutdown_timeout_seconds: float,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        self._use_case = use_case
        self._observer = observer
        self._shutdown_timeout = shutdown_timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def submit(self, reference: CompletedTurnReference, correlation_id: str) -> None:
        """Schedule one newly inserted turn; callers must not submit duplicates."""
        if self._closed:
            raise RuntimeError("memory formation runner is closed")
        task = asyncio.create_task(
            self._run(reference, correlation_id),
            name="kira-memory-formation",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for the current task set; primarily useful for lifecycle and tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks))

    async def aclose(self) -> None:
        """Drain submitted work up to a deadline, then cancel remaining tasks."""
        self._closed = True
        if not self._tasks:
            return
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await self.wait_idle()
        except TimeoutError:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        reference: CompletedTurnReference,
        correlation_id: str,
    ) -> None:
        started = perf_counter()
        try:
            result = await self._use_case.execute(reference)
        except asyncio.CancelledError:
            self._observer.memory_formation_observed(
                "cancelled",
                perf_counter() - started,
                0,
            )
            raise
        except ConversationStoreError as error:
            self._observer.memory_formation_observed("error", perf_counter() - started, 0)
            self._observer.degraded(
                correlation_id,
                "memory_source_read",
                type(error).__name__,
                "answer_without_ltm_write",
            )
        except LongTermMemoryError as error:
            self._observer.memory_formation_observed("error", perf_counter() - started, 0)
            self._observer.degraded(
                correlation_id,
                "memory_formation",
                type(error).__name__,
                "answer_without_ltm_write",
            )
        except Exception as error:
            # The task always consumes failures so they never surface as an SSE error or an
            # unhandled asyncio exception. Operational logs remain content-free.
            self._observer.memory_formation_observed("error", perf_counter() - started, 0)
            self._observer.degraded(
                correlation_id,
                "memory_formation",
                type(error).__name__,
                "answer_without_ltm_write",
            )
        else:
            self._observer.memory_formation_observed(
                "processed" if result.events else "no_change",
                perf_counter() - started,
                len(result.events),
            )
