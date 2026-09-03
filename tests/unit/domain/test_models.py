from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.models.chat import ChatCommand
from app.domain.models.conversation import ConversationMessage, ConversationRole
from app.domain.models.kira import KiraAuthResult
from app.presentation.schemas.chat import ChatRequest


@pytest.mark.parametrize(
    ("session_id", "message"),
    [("", "hello"), ("   ", "hello"), ("session", ""), ("session", "\t")],
)
def test_chat_command_rejects_blank_values(session_id: str, message: str) -> None:
    with pytest.raises(ValueError):
        ChatCommand(session_id=session_id, message=message)


def test_chat_request_strips_input_and_maps_to_domain() -> None:
    request = ChatRequest(session_id=" session-1 ", message=" Hưng Yên thì sao? ")

    assert request.to_command() == ChatCommand(
        session_id="session-1",
        message="Hưng Yên thì sao?",
    )


def test_chat_request_rejects_extra_identity_fields() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(session_id="session-1", message="hello", user_id="untrusted")  # type: ignore[call-arg]


def test_auth_result_hides_token_from_repr() -> None:
    result = KiraAuthResult(token="runtime-token", token_expiration_time=10621)

    assert result.token == "runtime-token"
    assert "runtime-token" not in repr(result)


def test_auth_result_rejects_empty_token() -> None:
    with pytest.raises(ValueError, match="token"):
        KiraAuthResult(token="")


def test_conversation_message_accepts_versioned_timezone_aware_content() -> None:
    message = ConversationMessage(
        session_id="session-1",
        turn_id="turn-1",
        role=ConversationRole.USER,
        content="Hưng Yên thì sao?",
        timestamp=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert message.schema_version == 1
    assert message.role is ConversationRole.USER


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": " "},
        {"turn_id": ""},
        {"content": "\t"},
        {"timestamp": datetime(2026, 9, 3)},
        {"role": "system"},
        {"schema_version": 2},
        {"schema_version": True},
        {"content": 123},
    ],
)
def test_conversation_message_rejects_invalid_values(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "role": ConversationRole.USER,
        "content": "question",
        "timestamp": datetime(2026, 9, 3, tzinfo=UTC),
    }
    values.update(changes)

    with pytest.raises(ValueError):
        ConversationMessage(**values)  # type: ignore[arg-type]
