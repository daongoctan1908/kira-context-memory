"""Deterministic fixture server: verifies orchestration, NEVER real model semantics."""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tests.support.week2_cases import CASES

app = FastAPI(title="Local vLLM Contract Stub")


@app.get("/health")
async def health() -> dict[str, str]:
    """Expose process liveness for the isolated Compose acceptance stack."""
    return {"status": "ok"}


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
    envelope = json.loads(body["messages"][1]["content"])
    current = envelope["current_query"]
    previous = envelope["recent_messages"][-2]["content"]
    output = next(
        (case.expected for case in CASES if case.current == current and case.previous == previous),
        current,
    )
    return JSONResponse({"choices": [{"finish_reason": "stop", "message": {"content": output}}]})
