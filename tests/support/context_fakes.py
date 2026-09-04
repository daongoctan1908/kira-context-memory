"""In-memory test doubles only; never used as runtime persistence or business answers."""

from datetime import UTC, datetime

from app.application.services.context_builder import ContextBuilder
from app.application.use_cases.handle_chat import HandleChatUseCase
from app.domain.models.context import ConversationContext
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.infrastructure.observability.context import ContextTelemetry


def pair(session_id="session-1", turn_id="old-turn", user="old query", assistant="old answer"):
    now = datetime.now(UTC)
    return (
        ConversationMessage(session_id, turn_id, ConversationRole.USER, user, now),
        ConversationMessage(session_id, turn_id, ConversationRole.ASSISTANT, assistant, now),
    )


class MemoryStore:
    def __init__(self, recent=(), *, read_error=None, write_error=None, inserted=True):
        self.recent = tuple(recent)
        self.read_error = read_error
        self.write_error = write_error
        self.inserted = inserted
        self.reads = []
        self.writes = []

    async def read_recent(self, session_id, limit):
        self.reads.append((session_id, limit))
        if self.read_error:
            raise self.read_error
        return self.recent[-limit:]

    async def append_turn(self, user, assistant):
        if self.write_error:
            raise self.write_error
        self.writes.append((user, assistant))
        self.recent += (user, assistant)
        return self.inserted


class FakeRewriter:
    def __init__(self, output="standalone query", error=None):
        self.output = output
        self.error = error
        self.contexts: list[ConversationContext] = []

    async def rewrite(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.output


def make_use_case(kira_client, **kwargs):
    defaults = {
        "conversation_store": MemoryStore(),
        "query_rewriter": FakeRewriter(),
        "context_builder": ContextBuilder(),
        "observer": ContextTelemetry(),
    }
    defaults.update(kwargs)
    return HandleChatUseCase(kira_client, **defaults)
