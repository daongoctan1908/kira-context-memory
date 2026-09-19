"""Domain models used by KiRa application ports."""

from app.domain.models.auth import AuthSession, AuthUser, AuthUserSummary, IssuedSession
from app.domain.models.chat import ChatCommand
from app.domain.models.context import ConversationContext
from app.domain.models.conversation import (
    CONVERSATION_MESSAGE_SCHEMA_VERSION,
    MAX_CHAT_MESSAGE_LENGTH,
    MAX_CONVERSATION_TITLE_LENGTH,
    AppendTurnResult,
    ChatRequestReservation,
    ChatRequestReservationOutcome,
    ChatRequestStatus,
    CompletedTurnReference,
    ConversationHistoryPage,
    ConversationListCursor,
    ConversationMessage,
    ConversationPage,
    ConversationRole,
    ConversationStatus,
    ConversationSummary,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.models.kira import KiraAuthResult, KiraEventKind, KiraStreamEvent
from app.domain.models.memory import (
    LongTermMemory,
    MemoryLifecycleEvent,
    MemoryProcessResult,
    MemorySource,
)
from app.domain.models.memory_job import (
    MEMORY_JOB_SCHEMA_VERSION,
    DeadMemoryJob,
    MemoryJob,
    MemoryJobPurgeResult,
    MemoryJobStats,
    MemoryJobStatus,
)
from app.domain.models.telemetry_context import (
    MAX_TELEMETRY_CONTEXT_BYTES,
    TELEMETRY_CONTEXT_VERSION,
    TelemetryContext,
)

__all__ = [
    "CONVERSATION_MESSAGE_SCHEMA_VERSION",
    "MAX_CONVERSATION_TITLE_LENGTH",
    "MAX_CHAT_MESSAGE_LENGTH",
    "AppendTurnResult",
    "ChatRequestReservation",
    "ChatRequestReservationOutcome",
    "ChatRequestStatus",
    "AuthSession",
    "AuthUser",
    "AuthUserSummary",
    "AuthenticatedPrincipal",
    "ChatCommand",
    "CompletedTurnReference",
    "ConversationContext",
    "ConversationHistoryPage",
    "ConversationListCursor",
    "ConversationMessage",
    "ConversationPage",
    "ConversationRole",
    "ConversationStatus",
    "ConversationSummary",
    "KiraAuthResult",
    "KiraEventKind",
    "KiraStreamEvent",
    "IssuedSession",
    "LongTermMemory",
    "MemoryLifecycleEvent",
    "MEMORY_JOB_SCHEMA_VERSION",
    "DeadMemoryJob",
    "MemoryJob",
    "MemoryJobPurgeResult",
    "MemoryJobStats",
    "MemoryJobStatus",
    "MemoryProcessResult",
    "MemorySource",
    "MAX_TELEMETRY_CONTEXT_BYTES",
    "TELEMETRY_CONTEXT_VERSION",
    "TelemetryContext",
]
