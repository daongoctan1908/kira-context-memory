"""Deterministic native-Mem0 extraction LLM for synthetic Compose acceptance."""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL = "local-memory-stub"
MEMORY_MARKER = "T4_E2E_MEMORY:"
_NEW_MESSAGES_HEADING = "## New Messages\n"
_NEXT_HEADING = "\n\n## Observation Date"

app = FastAPI(title="Local Memory LLM Contract Stub")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """Extract only explicitly marked synthetic facts into a native V3 envelope."""
    body = await request.json()
    messages = body.get("messages")
    if (
        body.get("model") != MODEL
        or body.get("temperature") != 0
        or body.get("max_tokens") != 1000
        or body.get("response_format") != {"type": "json_object"}
        or not isinstance(messages, list)
        or len(messages) != 2
        or not isinstance(messages[1], dict)
        or not isinstance(messages[1].get("content"), str)
    ):
        return JSONResponse(status_code=400, content={"error": "invalid contract"})

    facts = _extract_marked_facts(messages[1]["content"])
    content = json.dumps(
        {"memory": [{"text": fact, "attributed_to": "user"} for fact in facts]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return JSONResponse(
        {
            "id": "local-memory-completion",
            "object": "chat.completion",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


def _extract_marked_facts(prompt: str) -> tuple[str, ...]:
    normalized = prompt.replace("\r\n", "\n")
    try:
        serialized = normalized.split(_NEW_MESSAGES_HEADING, 1)[1].split(_NEXT_HEADING, 1)[0]
        messages = json.loads(serialized)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(messages, list):
        return ()

    facts: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.startswith(MEMORY_MARKER):
            continue
        fact = content.removeprefix(MEMORY_MARKER).strip()
        if fact and fact not in facts:
            facts.append(fact)
    return tuple(facts)
