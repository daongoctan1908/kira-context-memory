import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from starlette.requests import ClientDisconnect

from app.application.use_cases.handle_chat import ChatStreamSession
from app.domain.errors.kira import KiraTimeoutError
from app.domain.models.kira import KiraEventKind, KiraStreamEvent
from app.presentation.api.sse import ChatStreamingResponse, stream_gateway_events


def event(text: str) -> KiraStreamEvent:
    raw_data = json.dumps({"text": text}, ensure_ascii=False)
    return KiraStreamEvent(
        kind=KiraEventKind.TEXT,
        raw_data=raw_data,
        payload={"text": text},
        text_fragment=text,
    )


class GatedStream(AsyncIterator[KiraStreamEvent]):
    def __init__(self) -> None:
        self._index = 0
        self.release_second = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> "GatedStream":
        return self

    async def __anext__(self) -> KiraStreamEvent:
        if self._index == 0:
            self._index += 1
            return event("first")
        if self._index == 1:
            await self.release_second.wait()
            self._index += 1
            return event("second")
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class FailingStream(AsyncIterator[KiraStreamEvent]):
    def __init__(self) -> None:
        self._sent = False
        self.closed = False

    def __aiter__(self) -> "FailingStream":
        return self

    async def __anext__(self) -> KiraStreamEvent:
        if not self._sent:
            self._sent = True
            return event("partial")
        raise KiraTimeoutError(stage="chat stream")

    async def aclose(self) -> None:
        self.closed = True


async def test_gateway_yields_first_frame_without_waiting_for_second() -> None:
    source = GatedStream()
    session = ChatStreamSession(source)
    stream = stream_gateway_events(session, "correlation-1")

    first = await asyncio.wait_for(anext(stream), timeout=0.1)

    assert first == b'data: {"text": "first"}\n\n'
    assert not source.release_second.is_set()
    await stream.aclose()
    assert source.closed


async def test_gateway_maps_midstream_failure_to_typed_sse_error() -> None:
    source = FailingStream()
    session = ChatStreamSession(source)

    chunks = [chunk async for chunk in stream_gateway_events(session, "correlation-2")]

    assert chunks[0] == b'data: {"text": "partial"}\n\n'
    assert chunks[1].startswith(b"event: gateway_error\n")
    error_payload = json.loads(chunks[1].split(b"data: ", maxsplit=1)[1])
    assert error_payload == {
        "code": "KIRA_TIMEOUT",
        "message": "KiRa chat stream timed out",
        "correlation_id": "correlation-2",
        "retryable": True,
    }
    assert source.closed
    assert session.final_text == "partial"


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_client_disconnect_closes_suspended_stream_and_never_completes(spec_version):
    source = GatedStream()
    complete = AsyncMock()
    response = ChatStreamingResponse(ChatStreamSession(source, complete), "test", headers={})
    first_sent = asyncio.Event()

    async def receive():
        await first_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            first_sent.set()
            if spec_version == "2.4":
                raise OSError("client disconnected")

    async with asyncio.timeout(1):
        if spec_version == "2.4":
            with pytest.raises(ClientDisconnect):
                await response(
                    {"type": "http", "asgi": {"spec_version": spec_version}}, receive, send
                )
        else:
            await response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send)
    assert source.closed
    complete.assert_not_awaited()


async def test_unexpected_stream_error_is_sanitized():
    async def source():
        yield event("partial")
        raise RuntimeError("private content")

    frames = [frame async for frame in stream_gateway_events(ChatStreamSession(source()), "test")]
    assert len(frames) == 2
    assert b"gateway_error" in frames[1]
    assert b"private content" not in frames[1]
