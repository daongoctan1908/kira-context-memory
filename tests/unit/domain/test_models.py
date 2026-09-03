import pytest
from pydantic import ValidationError

from app.domain.models.chat import ChatCommand
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
