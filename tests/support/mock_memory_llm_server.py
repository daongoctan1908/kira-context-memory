"""Deterministic native-Mem0 extraction LLM for synthetic Compose acceptance."""

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tests.support.week4_cases import MEMORY_MARKER

MODEL = "local-memory-stub"
_NEW_MESSAGES_HEADING = "## New Messages\n"
_NEXT_HEADING = "\n\n## Observation Date"

_request_count = 0
_blocked_request_count = 0
_block_enabled = False
_release = asyncio.Event()
_release.set()

app = FastAPI(title="Local Memory LLM Contract Stub")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/_test/state")
async def state() -> dict[str, object]:
    """Return content-free synchronization evidence for the async E2E harness."""
    return {
        "stub": "memory-llm-local-only",
        "request_count": _request_count,
        "blocked_request_count": _blocked_request_count,
        "released": _release.is_set(),
    }


@app.post("/_test/reset")
async def reset(request: Request) -> JSONResponse:
    global _block_enabled, _blocked_request_count, _release, _request_count
    body = await request.json()
    block = body.get("block")
    if not isinstance(block, bool):
        return JSONResponse(status_code=400, content={"error": "invalid control"})
    _request_count = 0
    _blocked_request_count = 0
    _block_enabled = block
    _release = asyncio.Event()
    if not block:
        _release.set()
    return JSONResponse({"status": "reset", "blocking": block})


@app.post("/_test/release")
async def release() -> dict[str, str]:
    _release.set()
    return {"status": "released"}


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

    global _blocked_request_count, _request_count
    _request_count += 1
    if _block_enabled and not _release.is_set():
        _blocked_request_count += 1
        await _release.wait()

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
    except (IndexError, TypeError, ValueError):
        return ()

    messages = _new_messages(serialized)

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


def _new_messages(serialized: str) -> tuple[dict[str, str], ...]:
    try:
        parsed = json.loads(serialized)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return tuple(message for message in parsed if isinstance(message, dict))

    messages: list[dict[str, str]] = []
    for line in serialized.splitlines():
        role, separator, content = line.partition(": ")
        if separator and role in {"user", "assistant", "system"}:
            messages.append({"role": role, "content": content})
    return tuple(messages)
