"""Short/long-term context orchestration; KiRa remains the only answer source."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Self
from uuid import uuid4

from app.application.services.context_builder import ContextBuilder
from app.domain.errors.conversation import ConversationStoreError, ConversationStoreProtocolError
from app.domain.errors.memory import (
    LongTermMemoryError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.errors.query_rewriter import QueryRewriterError
from app.domain.models.chat import ChatCommand
from app.domain.models.context import MAX_LONG_TERM_MEMORIES
from app.domain.models.conversation import AppendTurnResult, ConversationMessage, ConversationRole
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraStreamEvent
from app.domain.models.memory import LongTermMemory
from app.domain.ports.context_observer import ContextObserverPort
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.kira_client import KiraClientPort
from app.domain.ports.long_term_memory import LongTermMemoryPort
from app.domain.ports.query_rewriter import QueryRewriterPort


class ChatStreamSession(AsyncIterator[KiraStreamEvent]):
    """Request-scoped stream that retains the ordered final assistant text."""

    def __init__(
        self,
        source: AsyncIterator[KiraStreamEvent],
        on_complete: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._source = source
        self._text_fragments: list[str] = []
        self._closed = False
        self._source_closed = False
        self._on_complete = on_complete
        self._completion_started = False

    @property
    def final_text(self) -> str:
        """Return text chunks received so far in their original order."""
        return "".join(self._text_fragments)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> KiraStreamEvent:
        if self._closed:
            raise StopAsyncIteration
        try:
            event = await anext(self._source)
        except StopAsyncIteration:
            await self.aclose()
            await self._complete_once()
            raise
        except BaseException:
            await self.aclose()
            raise
        if event.text_fragment is not None:
            self._text_fragments.append(event.text_fragment)
        return event

    async def aclose(self) -> None:
        """Close the downstream iterator when the client disconnects or cancels."""
        self._closed = True
        if self._source_closed:
            return
        close = getattr(self._source, "aclose", None)
        if close is not None:
            await close()
        self._source_closed = True

    async def _complete_once(self) -> None:
        """Invoke completion at most once, and only after clean source exhaustion."""
        if self._completion_started or self._on_complete is None or not self.final_text.strip():
            return
        # Set before awaiting so concurrent end-of-stream observation cannot schedule twice.
        self._completion_started = True
        await self._on_complete(self.final_text)


class HandleChatUseCase:
    """Read/rewrite with availability-first fallback, then persist completed pairs."""

    def __init__(
        self,
        kira_client: KiraClientPort,
        *,
        conversation_store: ConversationStorePort,
        query_rewriter: QueryRewriterPort,
        context_builder: ContextBuilder,
        observer: ContextObserverPort,
        max_recent_messages: int = 10,
        store_timeout_seconds: float = 5.0,
        long_term_memory: LongTermMemoryPort | None = None,
        memory_search_top_k: int = 10,
        memory_search_threshold: float = 0.1,
        memory_search_timeout_seconds: float = 3.0,
        memory_formation_enabled: bool = False,
    ) -> None:
        if (
            isinstance(memory_search_top_k, bool)
            or not isinstance(memory_search_top_k, int)
            or not 1 <= memory_search_top_k <= MAX_LONG_TERM_MEMORIES
        ):
            raise ValueError(f"memory_search_top_k must be between 1 and {MAX_LONG_TERM_MEMORIES}")
        if (
            isinstance(memory_search_threshold, bool)
            or not isinstance(memory_search_threshold, (int, float))
            or not 0 <= memory_search_threshold <= 1
        ):
            raise ValueError("memory_search_threshold must be between zero and one")
        if (
            isinstance(memory_search_timeout_seconds, bool)
            or not isinstance(memory_search_timeout_seconds, (int, float))
            or memory_search_timeout_seconds <= 0
        ):
            raise ValueError("memory_search_timeout_seconds must be positive")
        if not isinstance(memory_formation_enabled, bool):
            raise ValueError("memory_formation_enabled must be a boolean")
        self._kira_client = kira_client
        self._store = conversation_store
        self._rewriter = query_rewriter
        self._builder = context_builder
        self._observer = observer
        self._recent_limit = max_recent_messages
        self._store_timeout = store_timeout_seconds
        self._memory = long_term_memory
        self._memory_search_top_k = memory_search_top_k
        self._memory_search_threshold = float(memory_search_threshold)
        self._memory_search_timeout = float(memory_search_timeout_seconds)
        self._memory_formation_enabled = memory_formation_enabled

    async def execute(
        self,
        command: ChatCommand,
        *,
        principal: AuthenticatedPrincipal | None = None,
        correlation_id: str | None = None,
    ) -> ChatStreamSession:
        """Never persist a rewritten user query or a partial/failed assistant turn."""
        correlation_id = correlation_id or uuid4().hex
        turn_id = uuid4().hex
        started_at = datetime.now(UTC)
        if principal is None:
            self._observer.degraded(
                correlation_id,
                "identity",
                "IdentityUnavailable",
                "current_query_only",
            )
            self._observer.context_observed(0, 0)
            self._observer.memory_search_observed("bypass", None, None)
            self._observer.rewrite_observed("bypass", None)
            return ChatStreamSession(await self._kira_client.chat_stream(command.message))

        query = await self._resolve_query(command, principal.user_id, correlation_id)
        source = await self._kira_client.chat_stream(query)

        async def persist(final_text: str) -> None:
            if not self._memory_formation_enabled:
                self._observer.memory_job_schedule_observed("disabled")
            try:
                user = ConversationMessage(
                    command.session_id,
                    turn_id,
                    ConversationRole.USER,
                    command.message,
                    started_at,
                )
                assistant = ConversationMessage(
                    command.session_id,
                    turn_id,
                    ConversationRole.ASSISTANT,
                    final_text,
                    datetime.now(UTC),
                )
                async with asyncio.timeout(self._store_timeout):
                    result = await self._store.append_turn(
                        principal.user_id,
                        user,
                        assistant,
                        schedule_memory=self._memory_formation_enabled,
                    )
            except Exception as error:
                # Never turn a persistence failure into a synthetic SSE error. Cancellation
                # intentionally propagates and must roll back an in-flight transaction.
                self._observer.degraded(
                    correlation_id,
                    "postgres_write",
                    type(error).__name__,
                    "answer_without_history",
                )
                self._observer.conversation_write_observed("error")
                if self._memory_formation_enabled:
                    self._observer.memory_job_schedule_observed("error")
            else:
                self._observer.conversation_write_observed(
                    "inserted" if result.inserted else "duplicate"
                )
                if self._memory_formation_enabled:
                    self._observe_memory_job_schedule(correlation_id, result)

        return ChatStreamSession(source, on_complete=persist)

    def _observe_memory_job_schedule(
        self,
        correlation_id: str,
        result: AppendTurnResult,
    ) -> None:
        if not result.inserted:
            self._observer.memory_job_schedule_observed("duplicate")
            return
        if result.memory_job_event_id is not None:
            self._observer.memory_job_schedule_observed("scheduled")
            return
        # A newly inserted turn requested atomic scheduling, so a missing event ID
        # means the store broke its completion contract even if the answer was saved.
        self._observer.memory_job_schedule_observed("error")
        self._observer.degraded(
            correlation_id,
            "postgres_write",
            "ConversationStoreProtocolError",
            "answer_without_memory_job",
        )

    async def _resolve_query(
        self,
        command: ChatCommand,
        user_id: str,
        correlation_id: str,
    ) -> str:
        recent_result, memory_result = await self._load_context_inputs(
            user_id,
            command.session_id,
            command.message,
            correlation_id,
        )

        if isinstance(recent_result, (ConversationStoreError, TimeoutError)):
            self._observer.degraded(
                correlation_id,
                "postgres_read",
                type(recent_result).__name__,
                "original_query",
            )
            self._observer.context_observed(0, 0)
            self._observer.rewrite_observed("bypass", None)
            return command.message

        try:
            if any(message.session_id != command.session_id for message in recent_result):
                raise ConversationStoreProtocolError()
        except (AttributeError, TypeError, ConversationStoreProtocolError) as error:
            self._observer.degraded(
                correlation_id,
                "postgres_read",
                type(error).__name__,
                "original_query",
            )
            self._observer.context_observed(0, 0)
            self._observer.rewrite_observed("bypass", None)
            return command.message

        memories = () if isinstance(memory_result, LongTermMemoryError) else memory_result
        try:
            context = self._builder.build(recent_result, command.message, memories)
        except ValueError as error:
            # Store data already passed its session boundary. A remaining model invariant
            # failure can only make contextual rewrite unsafe, so fail closed to current-only.
            self._observer.degraded(
                correlation_id,
                "postgres_read",
                type(error).__name__,
                "original_query",
            )
            self._observer.context_observed(0, 0)
            self._observer.rewrite_observed("bypass", None)
            return command.message

        self._observer.context_observed(
            len(context.recent_messages), context.estimated_recent_tokens
        )
        if not context.recent_messages and not context.long_term_memories:
            self._observer.rewrite_observed("bypass", None)
            return command.message
        started = perf_counter()
        try:
            query = await self._rewriter.rewrite(context)
        except QueryRewriterError as error:
            self._observer.rewrite_observed("error", perf_counter() - started)
            self._observer.degraded(
                correlation_id,
                "rewriter",
                type(error).__name__,
                "original_query",
            )
            return command.message
        self._observer.rewrite_observed("success", perf_counter() - started)
        return query

    async def _load_context_inputs(
        self,
        user_id: str,
        session_id: str,
        query: str,
        correlation_id: str,
    ) -> tuple[
        tuple[ConversationMessage, ...] | ConversationStoreError | TimeoutError,
        tuple[LongTermMemory, ...] | LongTermMemoryError,
    ]:
        tasks = (
            asyncio.create_task(self._read_recent(user_id, session_id)),
            asyncio.create_task(self._search_memory(user_id, query, correlation_id)),
        )
        try:
            recent_result, memory_result = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return recent_result, memory_result

    async def _read_recent(
        self,
        user_id: str,
        session_id: str,
    ) -> tuple[ConversationMessage, ...] | ConversationStoreError | TimeoutError:
        try:
            async with asyncio.timeout(self._store_timeout):
                return await self._store.read_recent(
                    user_id,
                    session_id,
                    self._recent_limit,
                )
        except (ConversationStoreError, TimeoutError) as error:
            return error

    async def _search_memory(
        self,
        user_id: str,
        query: str,
        correlation_id: str,
    ) -> tuple[LongTermMemory, ...] | LongTermMemoryError:
        if self._memory is None:
            self._observer.memory_search_observed("bypass", None, None)
            return ()
        started = perf_counter()
        try:
            async with asyncio.timeout(self._memory_search_timeout):
                memories = await self._memory.search(
                    user_id,
                    query,
                    top_k=self._memory_search_top_k,
                    threshold=self._memory_search_threshold,
                )
        except TimeoutError:
            error = LongTermMemoryTimeoutError()
            self._observe_memory_failure(correlation_id, error, perf_counter() - started)
            return error
        except LongTermMemoryError as error:
            self._observe_memory_failure(correlation_id, error, perf_counter() - started)
            return error
        except Exception:
            self._observer.memory_search_observed("error", None, perf_counter() - started)
            raise

        if not isinstance(memories, tuple) or any(
            not isinstance(memory, LongTermMemory) for memory in memories
        ):
            error = LongTermMemoryProtocolError()
            self._observe_memory_failure(correlation_id, error, perf_counter() - started)
            return error
        self._observer.memory_search_observed(
            "success",
            len(memories),
            perf_counter() - started,
        )
        return memories

    def _observe_memory_failure(
        self,
        correlation_id: str,
        error: LongTermMemoryError,
        seconds: float,
    ) -> None:
        self._observer.memory_search_observed("error", None, seconds)
        self._observer.degraded(
            correlation_id,
            "memory_search",
            type(error).__name__,
            "recent_or_original_query",
        )
