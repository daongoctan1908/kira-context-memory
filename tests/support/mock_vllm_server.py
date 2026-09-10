"""Deterministic fixture server: verifies orchestration, NEVER real model semantics."""

import json
from collections import deque
from hashlib import sha256

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tests.support.week2_cases import CASES
from tests.support.week4_cases import (
    FOLLOW_UP_MARKER,
    memory_fact,
    rewritten_follow_up,
)

app = FastAPI(title="Local vLLM Contract Stub")
rewrite_evidence: deque[dict[str, object]] = deque(maxlen=100)


@app.get("/health")
async def health() -> dict[str, str]:
    """Expose process liveness for the isolated Compose acceptance stack."""
    return {"status": "ok"}


@app.get("/_test/requests")
async def observed_requests() -> dict[str, object]:
    """Expose only hashes and bounded context counts for synthetic E2E evidence."""
    return {"stub": "rewriter-local-only", "requests": list(rewrite_evidence)}


@app.post("/_test/reset")
async def reset_requests() -> dict[str, str]:
    rewrite_evidence.clear()
    return {"status": "reset"}


@app.post("/v1/embeddings")
async def embed(request: Request) -> JSONResponse:
    """Synthetic OpenAI-compatible embedding contract for local admin-init smoke."""
    body = await request.json()
    if body.get("model") != "local-embedding-stub" or "dimensions" in body:
        return JSONResponse(status_code=400, content={"error": "invalid contract"})
    return JSONResponse(
        {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "model": "local-embedding-stub",
        }
    )


@app.post("/v1/chat/completions")
async def rewrite(request: Request) -> JSONResponse:
    body = await request.json()
    if (
        body.get("model") != "local-contract-stub"
        or body.get("temperature") != 0
        or body.get("stream") is not False
        or body.get("max_tokens") != 256
    ):
        return JSONResponse(status_code=400, content={"error": "invalid contract"})
    try:
        envelope = json.loads(body["messages"][1]["content"])
        current = envelope["current_query"]
        recent = envelope["recent_messages"]
        memories = envelope.get("long_term_memories", [])
        if (
            not isinstance(current, str)
            or not isinstance(recent, list)
            or not isinstance(memories, list)
        ):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "invalid envelope"})

    previous = ""
    if len(recent) >= 2 and isinstance(recent[-2], dict):
        value = recent[-2].get("content")
        previous = value if isinstance(value, str) else ""
    output = next(
        (case.expected for case in CASES if case.current == current and case.previous == previous),
        current,
    )
    run_id = _follow_up_run_id(current)
    if run_id is not None and memory_fact(run_id) in memories:
        output = rewritten_follow_up(run_id)

    rewrite_evidence.append(
        {
            "current_hash": sha256(current.encode()).hexdigest(),
            "long_term_memory_count": len(memories),
            "output_hash": sha256(output.encode()).hexdigest(),
            "recent_message_count": len(recent),
        }
    )
    return JSONResponse({"choices": [{"finish_reason": "stop", "message": {"content": output}}]})


def _follow_up_run_id(current: str) -> str | None:
    if not current.startswith(FOLLOW_UP_MARKER):
        return None
    run_id = current.removeprefix(FOLLOW_UP_MARKER).split(":", 1)[0].strip()
    return run_id or None
