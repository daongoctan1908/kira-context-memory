"""Authenticated conversation management endpoints."""

import base64
import binascii
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.domain.models.conversation import ConversationListCursor
from app.domain.ports.conversation_store import ConversationStorePort
from app.presentation.api.auth_dependencies import resolve_auth_session
from app.presentation.schemas.conversation import (
    ConversationDeletionResponse,
    ConversationHistoryResponse,
    ConversationListResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
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
