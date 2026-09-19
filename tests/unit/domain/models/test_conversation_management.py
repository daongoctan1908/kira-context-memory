"""Conversation management domain contract tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.models.conversation import (
    MAX_CONVERSATION_TITLE_LENGTH,
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
