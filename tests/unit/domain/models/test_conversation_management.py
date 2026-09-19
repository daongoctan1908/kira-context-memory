"""Conversation management domain contract tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.models.conversation import (
    MAX_CONVERSATION_TITLE_LENGTH,
    ChatRequestReservation,
    ChatRequestReservationOutcome,
    ChatRequestStatus,
    ConversationHistoryPage,
    ConversationListCursor,
    ConversationMessage,
    ConversationPage,
    ConversationRole,
    ConversationStatus,
    ConversationSummary,
)

_NOW = datetime(2026, 9, 19, tzinfo=UTC)


def _summary(**changes) -> ConversationSummary:
    values = {
        "conversation_id": uuid4(),
        "session_id": "public-session",
        "title": "Support request",
        "status": ConversationStatus.ACTIVE,
        "created_at": _NOW,
        "updated_at": _NOW,
        "last_message_at": None,
    }
    values.update(changes)
    return ConversationSummary(**values)


def test_summary_uses_created_time_until_a_message_exists() -> None:
    summary = _summary()
    assert summary.activity_at == _NOW
    later = datetime(2026, 9, 19, 1, tzinfo=UTC)
    assert _summary(last_message_at=later).activity_at == later


@pytest.mark.parametrize(
    "changes",
    (
        {"session_id": ""},
        {"session_id": None},
        {"title": " "},
        {"title": 123},
        {"title": " padded "},
        {"title": "x" * (MAX_CONVERSATION_TITLE_LENGTH + 1)},
        {"status": "active"},
        {"created_at": datetime(2026, 9, 19)},
        {"created_at": "not-a-date"},
    ),
)
def test_summary_rejects_invalid_public_metadata(changes) -> None:
    with pytest.raises(ValueError):
        _summary(**changes)


def test_cursor_and_pages_validate_their_contracts() -> None:
    summary = _summary()
    cursor = ConversationListCursor(summary.activity_at, summary.conversation_id)
    assert ConversationPage((summary,), cursor).next_cursor == cursor
    message = ConversationMessage(
        "public-session",
        "turn-1",
        ConversationRole.USER,
        "hello",
        _NOW,
    )
    assert ConversationHistoryPage((message,), 1).next_before_message_id == 1

    with pytest.raises(ValueError):
        ConversationListCursor(datetime(2026, 9, 19), uuid4())
    with pytest.raises(ValueError):
        ConversationHistoryPage((message,), 0)


def test_chat_request_reservation_exposes_only_an_owned_attempt_lease() -> None:
    values = {
        "request_id": uuid4(),
        "conversation_id": uuid4(),
        "client_message_id": uuid4(),
        "turn_id": "turn-1",
        "status": ChatRequestStatus.PROCESSING,
        "outcome": ChatRequestReservationOutcome.ACQUIRED,
        "attempt_count": 1,
        "lease_token": uuid4(),
        "lease_expires_at": _NOW,
    }
    assert ChatRequestReservation(**values).lease_token == values["lease_token"]

    values.update(
        outcome=ChatRequestReservationOutcome.IN_PROGRESS,
        lease_token=None,
        lease_expires_at=None,
    )
    assert ChatRequestReservation(**values).lease_token is None

    values.update(outcome=ChatRequestReservationOutcome.ACQUIRED)
    with pytest.raises(ValueError, match="processing lease"):
        ChatRequestReservation(**values)

    values.update(
        outcome=ChatRequestReservationOutcome.COMPLETED,
        status=ChatRequestStatus.PROCESSING,
    )
    with pytest.raises(ValueError, match="completed state"):
        ChatRequestReservation(**values)
