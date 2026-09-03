"""Baseline chat orchestration without context or memory enrichment."""

from collections.abc import AsyncIterator
from typing import Self

from app.domain.models.chat import ChatCommand
from app.domain.models.kira import KiraStreamEvent
from app.domain.ports.kira_client import KiraClientPort


class ChatStreamSession(AsyncIterator[KiraStreamEvent]):
    """Request-scoped stream that retains the ordered final assistant text."""

    def __init__(self, source: AsyncIterator[KiraStreamEvent]) -> None:
        self._source = source
        self._text_fragments: list[str] = []
        self._closed = False

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
            raise
        if event.text_fragment is not None:
            self._text_fragments.append(event.text_fragment)
        return event

    async def aclose(self) -> None:
        """Close the downstream iterator when the client disconnects or cancels."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self._source, "aclose", None)
        if close is not None:
            await close()


class HandleChatUseCase:
    """Forward the current query to KiRa for the Week 1 baseline."""

    def __init__(self, kira_client: KiraClientPort) -> None:
        self._kira_client = kira_client

    async def execute(self, command: ChatCommand) -> ChatStreamSession:
        """Open the KiRa stream without adding recent context, LTM or rewriting."""
        source = await self._kira_client.chat_stream(command.message)
        return ChatStreamSession(source)
