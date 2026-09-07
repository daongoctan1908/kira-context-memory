"""Short-term context orchestration; KiRa remains the only answer source."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Self
from uuid import uuid4

from app.application.services.context_builder import ContextBuilder
from app.domain.errors.conversation import ConversationStoreError, ConversationStoreProtocolError
from app.domain.errors.query_rewriter import QueryRewriterError
from app.domain.models.chat import ChatCommand
from app.domain.models.conversation import (
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraStreamEvent
from app.domain.ports.context_observer import ContextObserverPort
from app.domain.ports.conversation_store import ConversationStorePort
from app.domain.ports.kira_client import KiraClientPort
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
            if self._on_complete is not None and self.final_text.strip():
                await self._on_complete(self.final_text)
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
        on_turn_completed: Callable[[CompletedTurnReference, str], None] | None = None,
    ) -> None:
        self._kira_client = kira_client
        self._store = conversation_store
        self._rewriter = query_rewriter
        self._builder = context_builder
        self._observer = observer
        self._recent_limit = max_recent_messages
        self._store_timeout = store_timeout_seconds
        self._on_turn_completed = on_turn_completed

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
            self._observer.rewrite_observed("bypass", None)
            return ChatStreamSession(await self._kira_client.chat_stream(command.message))

        query = await self._resolve_query(command, principal.user_id, correlation_id)
        source = await self._kira_client.chat_stream(query)

        async def persist(final_text: str) -> None:
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
                    result = await self._store.append_turn(principal.user_id, user, assistant)
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
            else:
                self._observer.conversation_write_observed(
                    "inserted" if result.inserted else "duplicate"
                )
                if result.inserted and self._on_turn_completed is not None:
                    try:
                        self._on_turn_completed(result.reference, correlation_id)
                    except Exception as error:
                        # Dispatch is optional and must never alter the KiRa answer contract.
                        self._observer.memory_formation_observed("error", 0, 0)
                        self._observer.degraded(
                            correlation_id,
                            "memory_dispatch",
                            type(error).__name__,
                            "answer_without_ltm_write",
                        )

        return ChatStreamSession(source, on_complete=persist)

    async def _resolve_query(
        self,
        command: ChatCommand,
        user_id: str,
        correlation_id: str,
    ) -> str:
        try:
            async with asyncio.timeout(self._store_timeout):
                recent = await self._store.read_recent(
                    user_id,
                    command.session_id,
                    self._recent_limit,
                )
            if any(message.session_id != command.session_id for message in recent):
                raise ConversationStoreProtocolError()
            context = self._builder.build(recent, command.message)
        except (ConversationStoreError, TimeoutError, ValueError) as error:
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
        if not context.recent_messages:
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
