"""Local KiRa contract stub used only for developer smoke verification."""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from hashlib import sha256

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="Local KiRa Contract Stub")
query_hashes: deque[str] = deque(maxlen=100)
_request_count = 0
_failure_count = 0
_remaining_failures = 0
_blocked_request_count = 0
_block_after_text_fragments: int | None = None
_release = asyncio.Event()
_release.set()


@app.get("/health")
async def health() -> dict[str, str]:
    """Expose process liveness for the isolated Compose acceptance stack."""
    return {"status": "ok"}


@app.get("/_test/requests")
async def observed_requests() -> dict[str, object]:
    """Test-only evidence, hashes of synthetic input instead of raw conversation text."""
    return {
        "stub": "kira-week2-local-only",
        "query_hashes": list(query_hashes),
        "request_count": _request_count,
        "failure_count": _failure_count,
        "remaining_failures": _remaining_failures,
        "blocked_request_count": _blocked_request_count,
        "released": _release.is_set(),
    }


@app.post("/_test/reset")
async def reset_requests(request: Request) -> JSONResponse:
    """Reset content-free evidence and configure bounded synthetic faults."""
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid control"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "invalid control"})
    block_after = body.get("block_after_text_fragments")
    failures = body.get("failures", 0)
    if (
        (
            block_after is not None
            and (
                isinstance(block_after, bool) or not isinstance(block_after, int) or block_after < 0
            )
        )
        or isinstance(failures, bool)
        or not isinstance(failures, int)
        or not 0 <= failures <= 100
    ):
        return JSONResponse(status_code=400, content={"error": "invalid control"})

    global _block_after_text_fragments, _blocked_request_count, _failure_count
    global _release, _remaining_failures, _request_count
    _release.set()
    query_hashes.clear()
    _request_count = 0
    _failure_count = 0
    _remaining_failures = failures
    _blocked_request_count = 0
    _block_after_text_fragments = block_after
    _release = asyncio.Event()
    if block_after is None:
        _release.set()
    return JSONResponse(
        {
            "status": "reset",
            "block_after_text_fragments": block_after,
            "remaining_failures": failures,
        }
    )


@app.post("/_test/release")
async def release_requests() -> dict[str, str]:
    _release.set()
    return {"status": "released"}


@app.post("/authenticate")
async def authenticate(request: Request) -> JSONResponse:
    payload = await request.json()
    if payload != {"username": "local-smoke", "domain": "VBI"}:
        return JSONResponse(status_code=400, content={"errorCode": "01"})
    return JSONResponse(
        content={
            "errorCode": "00",
            "description": None,
            "content": "local-runtime-token",
            "tokenExpirationTime": 300,
        }
    )


@app.post("/api/v1/chat")
async def chat(request: Request) -> Response:
    payload = await request.json()
    if payload.get("token") != "local-runtime-token" or payload.get("stream") is not True:
        return JSONResponse(status_code=401, content={"status": "unauthorized"})
    global _failure_count, _remaining_failures, _request_count
    _request_count += 1
    query_hashes.append(sha256(payload["message"]["text"].encode()).hexdigest())
    if _remaining_failures:
        _remaining_failures -= 1
        _failure_count += 1
        return JSONResponse(
            status_code=503,
            content={"status": "synthetic_unavailable"},
        )

    async def events() -> AsyncIterator[bytes]:
        global _blocked_request_count
        frames = [
            {
                "data": {
                    "response": [
                        {
                            "type": "status_response",
                            "content": {
                                "statusResponse": {
                                    "id": "step-1",
                                    "name": "Phân tích yêu cầu",
                                    "status": "completed",
                                }
                            },
                        }
                    ],
                    "requestId": "local-request-1",
                    "messageId": "local-message-1",
                },
                "type": 5,
            },
            {
                "data": {
                    "response": [{"type": "text", "content": {"text": "Mock "}}],
                    "requestId": "local-request-1",
                    "messageId": "local-message-1",
                },
                "type": 5,
            },
            {
                "data": {
                    "response": [{"type": "text", "content": {"text": "KiRa answer"}}],
                    "requestId": "local-request-1",
                    "messageId": "local-message-1",
                },
                "type": 5,
            },
        ]
        text_fragment_count = 0
        if _block_after_text_fragments == 0 and not _release.is_set():
            _blocked_request_count += 1
            await _release.wait()
        for frame in frames:
            yield f"data: {json.dumps(frame, separators=(',', ':'))}\n\n".encode()
            response_items = frame.get("data", {}).get("response", [])
            if any(item.get("type") == "text" for item in response_items):
                text_fragment_count += 1
                if _block_after_text_fragments == text_fragment_count and not _release.is_set():
                    _blocked_request_count += 1
                    await _release.wait()
            await asyncio.sleep(0.05)

    return StreamingResponse(events(), media_type="text/event-stream")
