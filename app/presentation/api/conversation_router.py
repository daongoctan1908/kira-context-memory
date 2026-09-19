"""Authenticated conversation management endpoints."""

import asyncio
import base64
import binascii
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.application.services.chat_idempotency import ChatIdempotencyService
from app.application.use_cases.handle_chat import HandleChatUseCase
from app.domain.errors.conversation import ChatRequestConflictError
from app.domain.models.chat import ChatCommand
from app.domain.models.conversation import (
    ChatRequestReservationOutcome,
    ConversationListCursor,
)
from app.domain.ports.conversation_store import ConversationStorePort
from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.tracing import set_request_span_attribute
from app.presentation.api.auth_dependencies import resolve_auth_session
from app.presentation.api.sse import ProductChatStreamingResponse
from app.presentation.schemas.conversation import (
    ConversationDeletionResponse,
    ConversationHistoryResponse,
    ConversationListResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    SendConversationMessageRequest,
)

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.post(
    "",
    response_model=ConversationSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: CreateConversationRequest,
    request: Request,
) -> ConversationSummaryResponse:
    session = await resolve_auth_session(request, require_csrf=True)
    store: ConversationStorePort = request.app.state.conversation_store
    created = await store.create_conversation(session.principal.user_id, title=body.title)
    return ConversationSummaryResponse.from_domain(created)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=256),
) -> ConversationListResponse:
    session = await resolve_auth_session(request, require_csrf=False)
    store: ConversationStorePort = request.app.state.conversation_store
    page = await store.list_conversations(
        session.principal.user_id,
        limit=limit,
        cursor=_decode_cursor(cursor) if cursor is not None else None,
    )
    next_cursor = _encode_cursor(page.next_cursor) if page.next_cursor is not None else None
    return ConversationListResponse.from_domain(page, next_cursor=next_cursor)


@router.get("/{session_id}/messages", response_model=ConversationHistoryResponse)
async def read_conversation_history(
    session_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    before_message_id: int | None = Query(default=None, ge=1),
) -> ConversationHistoryResponse:
    session = await resolve_auth_session(request, require_csrf=False)
    store: ConversationStorePort = request.app.state.conversation_store
    page = await store.read_history(
        session.principal.user_id,
        session_id,
        limit=limit,
        before_message_id=before_message_id,
    )
    if page is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return ConversationHistoryResponse.from_domain(page)


@router.post(
    "/{session_id}/messages",
    response_class=StreamingResponse,
    responses={409: {"description": "Request is already processing or conflicts"}},
)
async def send_conversation_message(
    session_id: str,
    body: SendConversationMessageRequest,
    request: Request,
) -> StreamingResponse:
    """Stream one idempotent product turn for an existing active conversation."""
    auth_session = await resolve_auth_session(request, require_csrf=True)
    user_id = auth_session.principal.user_id
    correlation_id: str = request.state.correlation_id
    coordinator: ChatIdempotencyService = request.app.state.chat_idempotency
    try:
        reservation = await coordinator.reserve(
            user_id,
            session_id,
            body.client_message_id,
            body.message,
        )
    except ChatRequestConflictError:
        raise HTTPException(status_code=409, detail="idempotency_conflict") from None
    if reservation is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    if reservation.outcome is ChatRequestReservationOutcome.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="request_in_progress")

    request.state.turn_id = reservation.turn_id
    set_request_span_attribute("turn_id", reservation.turn_id)
    set_request_span_attribute("client_message_id", str(body.client_message_id))
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Correlation-ID": correlation_id,
    }
    store: ConversationStorePort = request.app.state.conversation_store
    if reservation.outcome is ChatRequestReservationOutcome.COMPLETED:
        completed = await store.read_completed_chat_request(
            user_id,
            session_id,
            reservation,
        )
        if completed is None:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        return ProductChatStreamingResponse(
            correlation_id=correlation_id,
            turn_id=reservation.turn_id,
            client_message_id=str(body.client_message_id),
            replayed_message=completed[1],
            headers=headers,
        )

    use_case: HandleChatUseCase = request.app.state.handle_chat
    with bind_observability_context(
        correlation_id=correlation_id,
        turn_id=reservation.turn_id,
    ):
        try:
            stream = await use_case.execute_reserved(
                ChatCommand(session_id=session_id, message=body.message),
                reservation,
                principal=auth_session.principal,
                correlation_id=correlation_id,
            )
        except BaseException as error:
            try:
                await coordinator.abandon(
                    reservation,
                    cancelled=isinstance(error, asyncio.CancelledError),
                )
            except Exception:
                pass
            raise

    return ProductChatStreamingResponse(
        correlation_id=correlation_id,
        turn_id=reservation.turn_id,
        client_message_id=str(body.client_message_id),
        session=stream,
        on_abort=lambda cancelled: coordinator.abandon(
            reservation,
            cancelled=cancelled,
        ),
        headers=headers,
    )


@router.delete(
    "/{session_id}",
    response_model=ConversationDeletionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_conversation_deletion(
    session_id: str,
    request: Request,
) -> ConversationDeletionResponse:
    session = await resolve_auth_session(request, require_csrf=True)
    store: ConversationStorePort = request.app.state.conversation_store
    found = await store.mark_deletion_pending(session.principal.user_id, session_id)
    if not found:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return ConversationDeletionResponse()


def _encode_cursor(cursor: ConversationListCursor) -> str:
    payload = f"{cursor.activity_at.isoformat()}|{cursor.conversation_id}".encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str) -> ConversationListCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        ).decode("ascii")
        timestamp, separator, conversation_id = decoded.partition("|")
        if not separator:
            raise ValueError
        return ConversationListCursor(
            datetime.fromisoformat(timestamp),
            UUID(conversation_id),
        )
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid_conversation_cursor") from None
