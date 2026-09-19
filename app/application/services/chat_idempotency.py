"""Request-scoped idempotency coordination without transport dependencies."""

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from app.domain.models.conversation import (
    ChatRequestReservation,
    ChatRequestStatus,
)
from app.domain.ports.conversation_store import ConversationStorePort


def hash_chat_content(content: str) -> bytes:
    """Return the stable exact-content digest stored with a client message ID."""
    if not isinstance(content, str) or not content:
        raise ValueError("chat content must not be empty")
    return sha256(content.encode("utf-8")).digest()


class ChatIdempotencyService:
    """Acquire and release database-fenced chat request attempts."""

    def __init__(
        self,
        store: ConversationStorePort,
        *,
        lease_seconds: float,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
            raise ValueError("chat request lease must be a number")
        if not 10 <= float(lease_seconds) <= 900:
            raise ValueError("chat request lease must be between 10 and 900 seconds")
        self._store = store
        self._lease_seconds = float(lease_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    async def reserve(
        self,
        user_id: str,
        session_id: str,
        client_message_id: UUID,
        content: str,
    ) -> ChatRequestReservation | None:
        return await self._store.reserve_chat_request(
            user_id,
            session_id,
            client_message_id,
            hash_chat_content(content),
            now=self._aware_now(),
            lease_seconds=self._lease_seconds,
        )

    async def abandon(
        self,
        reservation: ChatRequestReservation,
        *,
        cancelled: bool,
    ) -> bool:
        if reservation.lease_token is None:
            raise ValueError("only an owned request attempt can be abandoned")
        return await self._store.abandon_chat_request(
            reservation.request_id,
            reservation.lease_token,
            status=(ChatRequestStatus.CANCELLED if cancelled else ChatRequestStatus.FAILED),
            now=self._aware_now(),
        )

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("chat idempotency clock must be timezone-aware")
        return value
