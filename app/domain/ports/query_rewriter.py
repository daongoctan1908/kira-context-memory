"""Framework-independent port for rewriting a follow-up into a standalone query."""

from typing import Protocol

from app.domain.models.context import ConversationContext


class QueryRewriterPort(Protocol):
    async def rewrite(self, context: ConversationContext) -> str:
        """Return a standalone query, never a business answer; raise typed errors on failure."""
        ...
