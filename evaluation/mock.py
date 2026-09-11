"""Offline preflight doubles; reports explicitly label these as simulated."""

import json

import httpx

from evaluation.config import EvalConfig
from evaluation.models import Probe


def mock_response(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/embeddings"):
        body = json.loads(request.content)
        dimensions = body.get("dimensions", 3)
        return httpx.Response(
            200,
            json={
                "model": "mock-model",
                "data": [{"index": i, "embedding": [0.1] * dimensions} for i in range(2)],
            },
        )
    if request.url.path.endswith("/chat/completions"):
        body = json.loads(request.content)
        content = "Truy vấn giả lập."
        if "native Mem0 V3" in body["messages"][0]["content"]:
            content = json.dumps(
                {
                    "memory": [
                        {
                            "id": "0",
                            "text": "User prefers tables.",
                            "attributed_to": "user",
                        }
                    ]
                }
            )
        return httpx.Response(
            200,
            json={
                "model": "mock-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )
    if request.url.path == "/ready":
        return httpx.Response(200, json={"status": "ready"})
    if request.url.path == "/_test/requests":
        return httpx.Response(200, json={"stub": "kira-week2-local-only"})
    return httpx.Response(404)


async def mock_database(config: EvalConfig, probe: Probe, dimension: int | None = None) -> dict:
    return {}
