"""Typed errors exposed by outbound domain ports."""

from app.domain.errors.conversation import (
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

__all__ = [
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
]
