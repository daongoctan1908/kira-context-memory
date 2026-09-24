"""Two-branch scoped long-term-memory retrieval with raw-score merge."""

import asyncio
from uuid import UUID

from app.domain.errors.memory import LongTermMemoryProtocolError
from app.domain.models.memory import LongTermMemory
from app.domain.ports.long_term_memory import LongTermMemoryPort


class ScopedMemoryRetriever:
    """Search conversation-local and user-global branches independently, merge raw scores."""

    def __init__(self, memory: LongTermMemoryPort) -> None:
        self._memory = memory

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        conversation_id: UUID,
        top_k: int,
        threshold: float,
    ) -> tuple[LongTermMemory, ...]:
        results = await asyncio.gather(
            self._memory.search_scoped(
                user_id,
                query,
                conversation_id=conversation_id,
                scope="conversation",
                top_k=top_k,
                threshold=threshold,
            ),
            self._memory.search_scoped(
                user_id,
                query,
                conversation_id=conversation_id,
                scope="global",
                top_k=top_k,
                threshold=threshold,
            ),
            return_exceptions=True,
        )
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
        by_content: dict[str, str] = {}
        ordered: list[LongTermMemory] = []
        # Conversation-local records win exact-content ties over global ones.
        for memory in (*conversation, *global_memories):
            if memory.memory_id in by_id:
                continue
            content_key = " ".join(memory.content.casefold().split())
            if content_key in by_content:
                continue
            by_id[memory.memory_id] = memory
            by_content[content_key] = memory.memory_id
            ordered.append(memory)
        ordered.sort(key=lambda memory: (-memory.score, memory.memory_id))
        return tuple(ordered[:top_k])
