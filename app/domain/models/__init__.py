"""Domain models used by KiRa application ports."""

from app.domain.models.chat import ChatCommand
from app.domain.models.context import ConversationContext
from app.domain.models.conversation import (
    CONVERSATION_MESSAGE_SCHEMA_VERSION,
    AppendTurnResult,
    CompletedTurnReference,
    ConversationMessage,
    ConversationRole,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraAuthResult, KiraEventKind, KiraStreamEvent
from app.domain.models.memory import (
    LongTermMemory,
    MemoryLifecycleEvent,
    MemoryProcessResult,
    MemorySource,
)

__all__ = [
    "CONVERSATION_MESSAGE_SCHEMA_VERSION",
    "AppendTurnResult",
    "AuthenticatedPrincipal",
    "ChatCommand",
    "CompletedTurnReference",
    "ConversationContext",
    "ConversationMessage",
    "ConversationRole",
    "KiraAuthResult",
    "KiraEventKind",
    "KiraStreamEvent",
    "LongTermMemory",
    "MemoryLifecycleEvent",
    "MemoryProcessResult",
    "MemorySource",
]
