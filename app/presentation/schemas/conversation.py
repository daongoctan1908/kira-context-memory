"""Public conversation management request and response contracts."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.domain.models.conversation import (
    MAX_CHAT_MESSAGE_LENGTH,
    ConversationHistoryPage,
    ConversationPage,
    ConversationSummary,
    MessageFeedbackRating,
)

ConversationTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: ConversationTitle | None = None


class RenameConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: ConversationTitle


class MessageFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rating: Literal["up", "down"]

    def to_domain(self) -> MessageFeedbackRating:
        return MessageFeedbackRating(self.rating)


class MessageFeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    rating: Literal["up", "down"]


class SendConversationMessageRequest(BaseModel):
    """One idempotent message submitted to an existing owned conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_message_id: UUID
    message: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_CHAT_MESSAGE_LENGTH,
        ),
    ]


class ConversationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID
    session_id: str
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None

    @classmethod
    def from_domain(cls, value: ConversationSummary) -> "ConversationSummaryResponse":
        return cls(
            conversation_id=value.conversation_id,
            session_id=value.session_id,
            title=value.title,
            status=value.status.value,
            created_at=value.created_at,
            updated_at=value.updated_at,
            last_message_at=value.last_message_at,
        )


class ConversationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ConversationSummaryResponse, ...]
    next_cursor: str | None

    @classmethod
    def from_domain(
        cls,
        value: ConversationPage,
        *,
        next_cursor: str | None,
    ) -> "ConversationListResponse":
        return cls(
            items=tuple(ConversationSummaryResponse.from_domain(item) for item in value.items),
            next_cursor=next_cursor,
        )


class ConversationMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    role: str
    content: str
    timestamp: datetime
    feedback: Literal["up", "down"] | None = None


class ConversationHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ConversationMessageResponse, ...]
    next_before_message_id: int | None

    @classmethod
    def from_domain(cls, value: ConversationHistoryPage) -> "ConversationHistoryResponse":
        return cls(
            items=tuple(
                ConversationMessageResponse(
                    turn_id=message.turn_id,
                    role=message.role.value,
                    content=message.content,
                    timestamp=message.timestamp,
                    feedback=(message.feedback.value if message.feedback is not None else None),
                )
                for message in value.messages
            ),
            next_before_message_id=value.next_before_message_id,
        )
