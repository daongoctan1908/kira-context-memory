"""Sanitized failures exposed by the conversation-store port."""


class ConversationStoreError(Exception):
    """Base error for a failed recent-conversation store operation."""


class ConversationStoreConnectionError(ConversationStoreError):
    """The Gateway could not connect to the configured conversation store."""

    def __init__(self) -> None:
        super().__init__("Conversation store is unavailable")


class ConversationStoreConfigurationError(ConversationStoreError):
    """The conversation store is wired with invalid configuration or schema."""

    def __init__(self) -> None:
        super().__init__("Conversation store configuration is invalid")


class ConversationStoreOperationError(ConversationStoreError):
    """The store rejected an otherwise valid operation."""

    def __init__(self) -> None:
        super().__init__("Conversation store operation failed")


class ConversationStoreProtocolError(ConversationStoreError):
    """Stored data does not match the supported message schema."""

    def __init__(self) -> None:
        super().__init__("Conversation store returned invalid data")


class ChatRequestConflictError(ConversationStoreError):
    """A client message identifier was reused with different content."""

    def __init__(self) -> None:
        super().__init__("Chat request idempotency conflict")


class ChatRequestLeaseLostError(ConversationStoreError):
    """A stale or expired attempt tried to finalize a chat request."""

    def __init__(self) -> None:
        super().__init__("Chat request lease is no longer current")
