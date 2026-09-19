"""Bound product-chat request bodies before FastAPI parses JSON."""

import json
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.presentation.schemas.errors import GatewayError


class ChatBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_product_chat(scope):
            await self._app(scope, receive, send)
            return
        limit = _configured_limit(scope)
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _send_too_large(scope, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await _send_too_large(scope, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self._app(scope, replay_receive, send)


def _is_product_chat(scope: Scope) -> bool:
    path = scope.get("path")
    return bool(
        scope["type"] == "http"
        and scope.get("method") == "POST"
        and isinstance(path, str)
        and path.startswith("/api/v1/conversations/")
        and path.endswith("/messages")
    )


def _configured_limit(scope: Scope) -> int:
    try:
        return int(scope["app"].state.settings.chat_max_body_bytes)
    except (AttributeError, KeyError, TypeError, ValueError):
        return 131_072


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name.lower() == b"content-length":
            try:
                parsed = int(value)
            except ValueError:
                return None
            return max(parsed, 0)
    return None


async def _send_too_large(scope: Scope, send: Send) -> None:
    correlation_id = scope.get("state", {}).get("correlation_id")
    if not isinstance(correlation_id, str):
        correlation_id = uuid4().hex
    payload = GatewayError(
        code="CHAT_BODY_TOO_LARGE",
        message="Chat request body is too large",
        correlation_id=correlation_id,
        retryable=False,
    )
    body = json.dumps(payload.model_dump(), separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": (
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ),
        }
    )
    await send({"type": "http.response.body", "body": body})
