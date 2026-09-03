"""Sanitized error payloads exposed by the Gateway."""

from pydantic import BaseModel, ConfigDict


class GatewayError(BaseModel):
    """Stable error shape used by HTTP and mid-stream SSE failures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    correlation_id: str
    retryable: bool
