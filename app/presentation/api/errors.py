"""Map typed KiRa failures to sanitized Gateway contracts."""

import json
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from app.domain.errors.kira import KiraClientError, KiraTimeoutError
from app.presentation.schemas.errors import GatewayError


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
