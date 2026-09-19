"""Map typed KiRa failures to sanitized Gateway contracts."""

import json
from uuid import uuid4

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.errors.auth import (
    AuthError,
    AuthStoreError,
    InvalidCredentialsError,
    InvalidCsrfTokenError,
    PasswordPolicyError,
)
from app.domain.errors.chat import (
    ChatAdmissionError,
    ChatConcurrencyLimitError,
    ChatRateLimitExceededError,
)
from app.domain.errors.conversation import (
    ChatRequestConflictError,
    ChatRequestLeaseLostError,
    ConversationStoreConfigurationError,
    ConversationStoreConnectionError,
    ConversationStoreError,
)
from app.domain.errors.kira import KiraClientError, KiraTimeoutError
from app.infrastructure.observability.tracing import (
    mark_request_outcome,
    set_request_span_attribute,
)
from app.presentation.schemas.errors import GatewayError


class ProductApiError(Exception):
    """Stable product API failure without internal implementation details."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", uuid4().hex)
    payload = GatewayError(
        code=code,
        message=message,
        correlation_id=correlation_id,
        retryable=retryable,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(),
        headers={"X-Correlation-ID": correlation_id},
    )


async def product_api_exception_handler(
    request: Request,
    error: ProductApiError,
) -> JSONResponse:
    return _error_response(
        request,
        status_code=error.status_code,
        code=error.code,
        message=str(error),
        retryable=error.retryable,
    )


async def chat_admission_exception_handler(
    request: Request,
    error: ChatAdmissionError,
) -> JSONResponse:
    if isinstance(error, ChatRateLimitExceededError):
        code, message = "CHAT_RATE_LIMITED", "Too many chat requests"
    elif isinstance(error, ChatConcurrencyLimitError):
        code, message = "CHAT_CONCURRENCY_LIMIT", "Too many active chat requests"
    else:
        code, message = "CHAT_ADMISSION_REJECTED", "Chat request rejected"
    return _error_response(
        request,
        status_code=429,
        code=code,
        message=message,
        retryable=True,
    )


async def conversation_store_exception_handler(
    request: Request,
    error: ConversationStoreError,
) -> JSONResponse:
    if isinstance(error, ChatRequestConflictError):
        status_code, code, message, retryable = (
            409,
            "IDEMPOTENCY_CONFLICT",
            "Client message ID was reused with different content",
            False,
        )
    elif isinstance(error, ChatRequestLeaseLostError):
        status_code, code, message, retryable = (
            409,
            "CHAT_ATTEMPT_EXPIRED",
            "Chat request attempt is no longer current",
            True,
        )
    elif isinstance(error, ConversationStoreConfigurationError):
        status_code, code, message, retryable = (
            503,
            "CHAT_STORE_MISCONFIGURED",
            "Chat storage is unavailable",
            False,
        )
    elif isinstance(error, ConversationStoreConnectionError):
        status_code, code, message, retryable = (
            503,
            "CHAT_STORE_UNAVAILABLE",
            "Chat storage is temporarily unavailable",
            True,
        )
    else:
        status_code, code, message, retryable = (
            503,
            "CHAT_STORE_ERROR",
            "Chat storage operation failed",
            True,
        )
    return _error_response(
        request,
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
    )


async def sanitized_validation_exception_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    if not request.url.path.startswith("/api/v1/"):
        return await request_validation_exception_handler(request, error)
    return _error_response(
        request,
        status_code=422,
        code="REQUEST_INVALID",
        message="Request validation failed",
        retryable=False,
    )


async def auth_exception_handler(request: Request, error: AuthError) -> JSONResponse:
    """Map authentication failures without exposing usernames, tokens or database details."""
    correlation_id = getattr(request.state, "correlation_id", uuid4().hex)
    if isinstance(error, InvalidCsrfTokenError):
        status_code, code, message, retryable = 403, "AUTH_CSRF_INVALID", "Request rejected", False
    elif isinstance(error, PasswordPolicyError):
        status_code, code, message, retryable = (
            422,
            "AUTH_PASSWORD_POLICY",
            "Password does not meet policy",
            False,
        )
    elif isinstance(error, AuthStoreError):
        status_code, code, message, retryable = (
            503,
            "AUTH_UNAVAILABLE",
            "Authentication temporarily unavailable",
            True,
        )
    elif isinstance(error, InvalidCredentialsError):
        status_code, code, message, retryable = (
            401,
            "AUTH_INVALID_CREDENTIALS",
            "Invalid credentials",
            False,
        )
    else:
        status_code, code, message, retryable = (
            401,
            "AUTH_SESSION_INVALID",
            "Authentication required",
            False,
        )
    payload = GatewayError(
        code=code,
        message=message,
        correlation_id=correlation_id,
        retryable=retryable,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(),
        headers={"X-Correlation-ID": correlation_id},
    )


def gateway_error(error: KiraClientError, correlation_id: str) -> GatewayError:
    """Create the public error payload without downstream response bodies or secrets."""
    return GatewayError(
        code=error.code,
        message=str(error) or "KiRa request failed",
        correlation_id=correlation_id,
        retryable=error.retryable,
    )


def gateway_error_status(error: KiraClientError) -> int:
    """Use 504 for downstream timeouts and 502 for other KiRa failures."""
    return 504 if isinstance(error, KiraTimeoutError) else 502


async def kira_client_exception_handler(
    request: Request,
    error: KiraClientError,
) -> JSONResponse:
    """Handle KiRa failures raised before client-facing SSE begins."""
    mark_request_outcome("error")
    set_request_span_attribute("error.type", type(error).__name__)
    correlation_id = getattr(request.state, "correlation_id", uuid4().hex)
    payload = gateway_error(error, correlation_id)
    return JSONResponse(
        status_code=gateway_error_status(error),
        content=payload.model_dump(),
        headers={"X-Correlation-ID": correlation_id},
    )


def encode_gateway_error_event(error: KiraClientError, correlation_id: str) -> bytes:
    """Encode a failure that occurs after the HTTP SSE response has started."""
    payload = gateway_error(error, correlation_id)
    data = json.dumps(payload.model_dump(), ensure_ascii=False, separators=(",", ":"))
    return f"event: gateway_error\ndata: {data}\n\n".encode()
