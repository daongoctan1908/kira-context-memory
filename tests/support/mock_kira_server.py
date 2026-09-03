"""Local KiRa contract stub used only for developer smoke verification."""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="Local KiRa Contract Stub")


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

    async def events() -> AsyncIterator[bytes]:
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
        for frame in frames:
            yield f"data: {json.dumps(frame, separators=(',', ':'))}\n\n".encode()
            await asyncio.sleep(0.05)

    return StreamingResponse(events(), media_type="text/event-stream")
