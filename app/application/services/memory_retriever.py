"""Two-branch scoped long-term-memory retrieval with raw-score merge."""

import asyncio
from time import perf_counter
from typing import Protocol
from uuid import UUID

from app.domain.errors.memory import LongTermMemoryProtocolError
from app.domain.models.memory import LongTermMemory
from app.domain.ports.long_term_memory import LongTermMemoryPort


class MemoryBranchObserver(Protocol):
    def memory_branch_observed(
        self, branch: str, outcome: str, result_count: int, seconds: float
    ) -> None: ...


class ScopedMemoryRetriever:
    """Search conversation-local and user-global branches independently, merge raw scores."""

    def __init__(
        self,
        memory: LongTermMemoryPort,
        observer: MemoryBranchObserver | None = None,
    ) -> None:
        self._memory = memory
        self._observer = observer

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        conversation_id: UUID,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        conversation_task = asyncio.create_task(
            self._search_branch("conversation", user_id, query, conversation_id, top_k, threshold)
        )
        global_task = asyncio.create_task(
            self._search_branch("global", user_id, query, conversation_id, top_k, threshold)
        )
        results = await asyncio.gather(conversation_task, global_task, return_exceptions=True)
        conversation_result, global_result = results
        if not isinstance(conversation_result, BaseException) and not self._valid(
            conversation_result
        ):
            conversation_result = LongTermMemoryProtocolError()
        if not isinstance(global_result, BaseException) and not self._valid(global_result):
            global_result = LongTermMemoryProtocolError()
        if isinstance(conversation_result, BaseException) and isinstance(
            global_result, BaseException
        ):
            # Both branches failed: surface the error so the caller keeps its
            # recent-only degraded fallback instead of silently empty context.
            raise conversation_result
        if isinstance(conversation_result, BaseException):
            return global_result
        if isinstance(global_result, BaseException):
            return conversation_result
        return self._merge(conversation_result, global_result, top_k)

    async def _search_branch(
        self,
        branch: str,
        user_id: str,
        query: str,
        conversation_id: UUID,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        started = perf_counter()
        try:
            result = await self._memory.search_scoped(
                user_id,
                query,
                conversation_id=conversation_id,
                scope=branch,
                top_k=top_k,
                threshold=threshold,
            )
        except Exception:
            self._observe_branch(branch, "error", 0, perf_counter() - started)
            raise
        self._observe_branch(
            branch,
            "success" if result else "empty",
            len(result),
            perf_counter() - started,
        )
        return result

    def _observe_branch(self, branch: str, outcome: str, result_count: int, seconds: float) -> None:
        if self._observer is None:
            return
        try:
            self._observer.memory_branch_observed(branch, outcome, result_count, seconds)
        except Exception:
            pass

    @staticmethod
    def _valid(result) -> bool:
        return isinstance(result, tuple) and all(
            isinstance(memory, LongTermMemory) for memory in result
        )

    @staticmethod
    def _merge(
        conversation: tuple[LongTermMemory, ...],
        global_memories: tuple[LongTermMemory, ...],
        top_k: int,
    ) -> tuple[LongTermMemory, ...]:
        by_id: dict[str, LongTermMemory] = {}
        ordered: list[LongTermMemory] = []
        # Repeated text can carry different scope or source evidence. Only the
        # same record is a duplicate; keep independent assertions available.
        for memory in (*conversation, *global_memories):
            if memory.memory_id in by_id:
                continue
            by_id[memory.memory_id] = memory
            ordered.append(memory)
        ordered.sort(key=lambda memory: (-memory.score, memory.memory_id))
        return tuple(ordered[:top_k])
