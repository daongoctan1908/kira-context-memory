"""Authenticated conversation management endpoints."""

import asyncio
import base64
import binascii
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

from app.application.services.chat_idempotency import ChatIdempotencyService
from app.application.services.chat_traffic import ChatTrafficGuard, ChatTrafficLease
from app.application.use_cases.handle_chat import HandleChatUseCase
from app.domain.models.chat import ChatCommand
from app.domain.models.conversation import (
    ChatRequestReservationOutcome,
    ConversationListCursor,
)
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.ports.conversation_store import ConversationStorePort
from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.tracing import set_request_span_attribute
from app.presentation.api.auth_dependencies import resolve_auth_session
from app.presentation.api.errors import ProductApiError
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
        raise ProductApiError(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
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
    traffic: ChatTrafficGuard = request.app.state.chat_traffic
    traffic_lease = await traffic.acquire(user_id)
    try:
        return await _start_product_chat(
            session_id=session_id,
            body=body,
            request=request,
            principal=auth_session.principal,
            correlation_id=correlation_id,
            traffic_lease=traffic_lease,
        )
    except BaseException:
        await traffic_lease.release()
        raise


async def _start_product_chat(
    *,
    session_id: str,
    body: SendConversationMessageRequest,
    request: Request,
    principal: AuthenticatedPrincipal,
    correlation_id: str,
    traffic_lease: ChatTrafficLease,
) -> StreamingResponse:
    user_id = principal.user_id
    coordinator: ChatIdempotencyService = request.app.state.chat_idempotency
    reservation = await coordinator.reserve(
        user_id,
        session_id,
        body.client_message_id,
        body.message,
    )
    if reservation is None:
        raise ProductApiError(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
    if reservation.outcome is ChatRequestReservationOutcome.IN_PROGRESS:
        raise ProductApiError(
            409,
            "REQUEST_IN_PROGRESS",
            "Chat request is already processing",
            retryable=True,
        )

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
            raise ProductApiError(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
        return ProductChatStreamingResponse(
            correlation_id=correlation_id,
            turn_id=reservation.turn_id,
            client_message_id=str(body.client_message_id),
            replayed_message=completed[1],
            on_finish=traffic_lease.release,
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
                principal=principal,
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

    async def abandon(cancelled: bool) -> None:
        await coordinator.abandon(reservation, cancelled=cancelled)

    return ProductChatStreamingResponse(
        correlation_id=correlation_id,
        turn_id=reservation.turn_id,
        client_message_id=str(body.client_message_id),
        session=stream,
        on_abort=abandon,
        on_finish=traffic_lease.release,
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
        raise ProductApiError(404, "CONVERSATION_NOT_FOUND", "Conversation not found")
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
        raise ProductApiError(
            422,
            "CONVERSATION_CURSOR_INVALID",
            "Conversation cursor is invalid",
        ) from None
