"""Chat request digest and fenced reservation service tests."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from app.application.services.chat_idempotency import (
    ChatIdempotencyService,
    hash_chat_content,
)
from app.domain.models.conversation import (
    ChatRequestReservation,
    ChatRequestReservationOutcome,
    ChatRequestStatus,
)

_NOW = datetime(2026, 9, 19, tzinfo=UTC)


class FakeStore:
    def __init__(self) -> None:
        self.reservations = []
        self.abandoned = []
        self.result = None

    async def reserve_chat_request(self, *args, **kwargs):
        self.reservations.append((args, kwargs))
        return self.result

    async def abandon_chat_request(self, *args, **kwargs):
        self.abandoned.append((args, kwargs))
        return True


def _reservation() -> ChatRequestReservation:
    return ChatRequestReservation(
        uuid4(),
        uuid4(),
        uuid4(),
        "turn-1",
        ChatRequestStatus.PROCESSING,
        ChatRequestReservationOutcome.ACQUIRED,
        1,
        uuid4(),
        _NOW,
    )


async def test_service_hashes_exact_content_and_forwards_bounded_lease() -> None:
    store = FakeStore()
    store.result = _reservation()
    service = ChatIdempotencyService(
        store,  # type: ignore[arg-type]
        lease_seconds=120,
        now=lambda: _NOW,
    )
    client_message_id = uuid4()

    assert (
        await service.reserve("user-1", "session-1", client_message_id, "Xin chào") is store.result
    )
    args, kwargs = store.reservations[0]
    assert args == (
        "user-1",
        "session-1",
        client_message_id,
        sha256("Xin chào".encode()).digest(),
    )
    assert kwargs == {"now": _NOW, "lease_seconds": 120.0}


async def test_abandon_maps_failure_and_cancellation_without_changing_token() -> None:
    store = FakeStore()
    service = ChatIdempotencyService(
        store,  # type: ignore[arg-type]
        lease_seconds=120,
        now=lambda: _NOW,
    )
    reservation = _reservation()

    assert await service.abandon(reservation, cancelled=False)
    assert store.abandoned[-1] == (
        (reservation.request_id, reservation.lease_token),
        {"status": ChatRequestStatus.FAILED, "now": _NOW},
    )
    assert await service.abandon(reservation, cancelled=True)
    assert store.abandoned[-1][1]["status"] is ChatRequestStatus.CANCELLED


def test_hash_and_service_configuration_reject_invalid_input() -> None:
    assert hash_chat_content("a") == sha256(b"a").digest()
    with pytest.raises(ValueError):
        hash_chat_content("")
    for seconds in (True, 9, 901):
        with pytest.raises(ValueError):
            ChatIdempotencyService(FakeStore(), lease_seconds=seconds)  # type: ignore[arg-type]
    service = ChatIdempotencyService(
        FakeStore(),  # type: ignore[arg-type]
        lease_seconds=120,
        now=lambda: datetime(2026, 9, 19),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        service._aware_now()
