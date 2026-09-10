"""Deterministic OpenAI-compatible embeddings for synthetic Compose acceptance."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL = "local-embedding-stub"
DIMENSIONS = 3

app = FastAPI(title="Local Embedding Contract Stub")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    """Return stable non-zero vectors for admin probe, formation, and retrieval."""
    body = await request.json()
    supplied_dimensions = body.get("dimensions")
    if body.get("model") != MODEL or supplied_dimensions not in (None, DIMENSIONS):
        return JSONResponse(status_code=400, content={"error": "invalid contract"})

    value = body.get("input")
    if isinstance(value, str):
        inputs = [value]
    elif isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        inputs = value
    else:
        return JSONResponse(status_code=400, content={"error": "invalid input"})

    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [1.0, 0.0, 0.0],
                }
                for index, _ in enumerate(inputs)
            ],
            "model": MODEL,
            "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
        }
    )
