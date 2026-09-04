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
    "QueryRewriterConfigurationError",
    "QueryRewriterConnectionError",
    "QueryRewriterError",
    "QueryRewriterHttpError",
    "QueryRewriterProtocolError",
    "QueryRewriterTimeoutError",
]
