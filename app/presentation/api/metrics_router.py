"""Low-cardinality operational metrics; no conversation identifiers or content."""

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=Response)
async def metrics(request: Request) -> Response:
    return Response(
        generate_latest(request.app.state.telemetry.registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
