"""Chat input models."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatCommand:
    """A validated request to send the current message to KiRa."""

    session_id: str
    message: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if not self.message.strip():
            raise ValueError("message must not be empty")
