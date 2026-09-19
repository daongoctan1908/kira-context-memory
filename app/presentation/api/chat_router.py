"""Context-aware chat endpoint with the unchanged public SSE contract."""

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.application.use_cases.handle_chat import HandleChatUseCase
from app.infrastructure.observability.context import bind_observability_context
from app.infrastructure.observability.tracing import set_request_span_attribute
from app.presentation.api.auth_dependencies import resolve_chat_principal
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
    correlation_id: str = request.state.correlation_id
    turn_id = uuid4().hex
    request.state.turn_id = turn_id
    set_request_span_attribute("turn_id", turn_id)
    use_case: HandleChatUseCase = request.app.state.handle_chat
    observer = request.app.state.telemetry
    with bind_observability_context(correlation_id=correlation_id, turn_id=turn_id):
        with observer.stage("identity.resolve") as observation:
            try:
                principal = await resolve_chat_principal(request)
            except BaseException:
                observation.set_outcome("error")
                raise
            observation.set_outcome("authenticated" if principal is not None else "anonymous")
        session = await use_case.execute(
            body.to_command(),
            principal=principal,
            correlation_id=correlation_id,
            turn_id=turn_id,
        )

    return ChatStreamingResponse(
        session,
        correlation_id,
        turn_id=turn_id,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Correlation-ID": correlation_id,
        },
    )
