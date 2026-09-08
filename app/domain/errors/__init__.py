"""Typed errors exposed by outbound domain ports."""

from app.domain.errors.conversation import (
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreError,
    ConversationStoreOperationError,
    ConversationStoreProtocolError,
)
from app.domain.errors.kira import (
    KiraAuthenticationError,
    KiraClientError,
    KiraConnectionError,
    KiraHttpError,
    KiraMalformedSseError,
    KiraProtocolError,
    KiraTimeoutError,
)
from app.domain.errors.memory import (
    LongTermMemoryConfigurationError,
    LongTermMemoryConnectionError,
    LongTermMemoryError,
    LongTermMemoryOperationError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)
from app.domain.errors.memory_job import (
    MemoryJobLeaseLostError,
    MemoryJobQueueConfigurationError,
    MemoryJobQueueConnectionError,
    MemoryJobQueueError,
    MemoryJobQueueOperationError,
    MemoryJobQueueProtocolError,
)
from app.domain.errors.query_rewriter import (
    QueryRewriterConfigurationError,
    QueryRewriterConnectionError,
    QueryRewriterError,
    QueryRewriterHttpError,
    QueryRewriterProtocolError,
    QueryRewriterTimeoutError,
)

__all__ = [
    "ConversationStoreConfigurationError",
    "ConversationStoreConnectionError",
    "ConversationStoreError",
    "ConversationStoreOperationError",
    "ConversationStoreProtocolError",
    "KiraAuthenticationError",
    "KiraClientError",
    "KiraConnectionError",
    "KiraHttpError",
    "KiraMalformedSseError",
    "KiraProtocolError",
    "KiraTimeoutError",
    "LongTermMemoryConfigurationError",
    "LongTermMemoryConnectionError",
    "LongTermMemoryError",
    "LongTermMemoryOperationError",
    "LongTermMemoryProtocolError",
    "LongTermMemoryTimeoutError",
    "MemoryJobLeaseLostError",
    "MemoryJobQueueConfigurationError",
    "MemoryJobQueueConnectionError",
    "MemoryJobQueueError",
    "MemoryJobQueueOperationError",
    "MemoryJobQueueProtocolError",
    "QueryRewriterConfigurationError",
    "QueryRewriterConnectionError",
    "QueryRewriterError",
    "QueryRewriterHttpError",
    "QueryRewriterProtocolError",
    "QueryRewriterTimeoutError",
]
