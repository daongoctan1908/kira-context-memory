"""Liveness and readiness probes for the Gateway process."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Report that the process event loop is serving requests."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Report local dependency initialization without probing KiRa availability."""
    is_ready = bool(getattr(request.app.state, "ready", False))
    store = getattr(request.app.state, "conversation_store", None)
    is_ready = is_ready and getattr(store, "status", None) != "misconfigured"
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "not_ready"},
    )
