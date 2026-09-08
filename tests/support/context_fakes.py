"""In-memory test doubles only; never used as runtime persistence or business answers."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.application.services.context_builder import ContextBuilder
from app.application.use_cases.handle_chat import HandleChatUseCase
from app.domain.models.context import ConversationContext
from app.domain.models.conversation import (
    AppendTurnResult,
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.infrastructure.observability.context import ContextTelemetry

PRINCIPAL = AuthenticatedPrincipal("test-user")


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
        self.read_users = []
        self.writes = []
        self.write_users = []
        self.schedule_requests = []
        self.conversation_id = uuid4()
        self.memory_job_event_id = uuid4()

    async def read_recent(self, user_id, session_id, limit):
        self.reads.append((session_id, limit))
        self.read_users.append(user_id)
        if self.read_error:
            raise self.read_error
        return self.recent[-limit:]

    async def append_turn(self, user_id, user, assistant, *, schedule_memory=False):
        if self.write_error:
            raise self.write_error
        self.writes.append((user, assistant))
        self.write_users.append(user_id)
        self.schedule_requests.append(schedule_memory)
        self.recent += (user, assistant)
        return AppendTurnResult(
            self.inserted,
            CompletedTurnReference(
                user_id,
                user.session_id,
                self.conversation_id,
                user.turn_id,
                len(self.recent),
            ),
            self.memory_job_event_id if schedule_memory else None,
        )

    async def read_through_boundary(
        self,
        user_id: str,
        conversation_id: UUID,
        boundary_message_id: int,
        limit: int,
    ):
        if user_id != PRINCIPAL.user_id or conversation_id != self.conversation_id:
            return ()
        return self.recent[:boundary_message_id][-limit:]


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
