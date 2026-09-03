"""Schemas exposed by the Gateway chat API."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.domain.models.chat import ChatCommand

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ChatRequest(BaseModel):
    """Baseline request accepted by ``POST /chat``.

    ``session_id`` is a conversation correlation key, not an authenticated user identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: NonEmptyString
    message: NonEmptyString

    def to_command(self) -> ChatCommand:
        """Convert the transport schema into a framework-independent command."""
        return ChatCommand(session_id=self.session_id, message=self.message)
