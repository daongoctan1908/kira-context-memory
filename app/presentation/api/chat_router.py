"""Context-aware chat endpoint with the unchanged Week 1 SSE contract."""

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.application.use_cases.handle_chat import HandleChatUseCase
from app.domain.ports.identity import IdentityPort
from app.presentation.api.sse import ChatStreamingResponse
from app.presentation.schemas.chat import ChatRequest
from app.presentation.schemas.errors import GatewayError

router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_class=StreamingResponse,
    responses={
        502: {"model": GatewayError, "description": "KiRa downstream failure"},
        504: {"model": GatewayError, "description": "KiRa downstream timeout"},
    },
)
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    """Forward the current message to KiRa and proxy its SSE frames."""
    correlation_id = uuid4().hex
    request.state.correlation_id = correlation_id
    use_case: HandleChatUseCase = request.app.state.handle_chat
    identity: IdentityPort = request.app.state.identity_provider
    principal = await identity.resolve()
    session = await use_case.execute(
        body.to_command(),
        principal=principal,
        correlation_id=correlation_id,
    )

    return ChatStreamingResponse(
        session,
        correlation_id,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Correlation-ID": correlation_id,
        },
    )
