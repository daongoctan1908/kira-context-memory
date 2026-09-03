import json
from collections.abc import AsyncIterator

import httpx
import pytest

from app.config.settings import Settings
from app.domain.errors.kira import (
    KiraAuthenticationError,
    KiraHttpError,
    KiraMalformedSseError,
    KiraTimeoutError,
)
from app.domain.models.kira import KiraEventKind
from app.infrastructure.kira.http_kira_client import KiraHttpAdapter


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "kira_base_url": "http://kira.test:8122",
        "kira_username": "service-account",
        "kira_basic_auth": "basic-credential",
        "kira_domain": "VBI",
        "kira_service_id": 5,
        "kira_device": "Browser",
        "kira_message_type": "text",
        "kira_connect_timeout_seconds": 5,
        "kira_read_timeout_seconds": 300,
        "kira_token_expiry_skew_seconds": 60,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def auth_response(token: str = "runtime-token", ttl: float | None = 10621) -> dict[str, object]:
    return {
        "errorCode": "00",
        "description": None,
        "content": token,
        "tokenExpirationTime": ttl,
    }


def sse_payload(text: str) -> dict[str, object]:
    return {
        "data": {
            "response": [{"type": "text", "content": {"text": text}}],
            "requestId": "request-1",
            "messageId": "message-1",
        },
        "type": 5,
    }


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        self.closed = True


async def test_authenticate_uses_confirmed_request_and_parses_token() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=auth_response(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        result = await adapter.authenticate()

    assert result.token == "runtime-token"
    assert result.token_expiration_time == 10621.0
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://kira.test:8122/authenticate"
    assert request.headers["Authorization"] == "Basic basic-credential"
    assert json.loads(request.content) == {"username": "service-account", "domain": "VBI"}


@pytest.mark.parametrize(
    "payload",
    [
        {"errorCode": "01", "description": "rejected", "content": None},
        {"errorCode": "00", "content": None},
        {"errorCode": "00", "content": "   "},
    ],
)
async def test_authenticate_rejects_business_failure_or_missing_token(
    payload: dict[str, object],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        with pytest.raises(KiraAuthenticationError):
            await adapter.authenticate()


async def test_chat_stream_sends_exact_payload_and_yields_frames_in_order() -> None:
    requests: list[httpx.Request] = []
    first = json.dumps(sse_payload("Dạ "), ensure_ascii=False).encode()
    second = json.dumps(sse_payload("đúng rồi"), ensure_ascii=False).encode()
    stream = TrackingStream(b"data: " + first + b"\n\n", b"data: " + second + b"\n\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        iterator = await adapter.chat_stream("Hưng Yên thì sao?")
        events = [event async for event in iterator]

    assert [event.kind for event in events] == [KiraEventKind.TEXT, KiraEventKind.TEXT]
    assert "".join(event.text_fragment or "" for event in events) == "Dạ đúng rồi"
    assert stream.closed
    assert len(requests) == 2
    chat_request = requests[1]
    assert str(chat_request.url) == "http://kira.test:8122/api/v1/chat"
    assert chat_request.headers["Authorization"] == "Basic basic-credential"
    assert json.loads(chat_request.content) == {
        "sender": {"data": None, "domain": "VBI", "device": "Browser"},
        "service": 5,
        "content": None,
        "message": {"text": "Hưng Yên thì sao?", "type": "text"},
        "token": "runtime-token",
        "stream": True,
    }


async def test_chat_stream_reuses_cached_token() -> None:
    auth_calls = 0
    chat_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls, chat_calls
        if request.url.path == "/authenticate":
            auth_calls += 1
            return httpx.Response(200, json=auth_response(), request=request)
        chat_calls += 1
        return httpx.Response(200, stream=TrackingStream(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings(), clock=lambda: 100.0)
        first = await adapter.chat_stream("first")
        second = await adapter.chat_stream("second")
        assert [event async for event in first] == []
        assert [event async for event in second] == []

    assert auth_calls == 1
    assert chat_calls == 2


async def test_chat_stream_refreshes_once_on_unauthorized_before_forwarding() -> None:
    auth_calls = 0
    chat_tokens: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if request.url.path == "/authenticate":
            auth_calls += 1
            return httpx.Response(
                200,
                json=auth_response(token=f"token-{auth_calls}"),
                request=request,
            )

        chat_tokens.append(json.loads(request.content)["token"])
        if len(chat_tokens) == 1:
            return httpx.Response(401, request=request)
        data = json.dumps(sse_payload("ok")).encode()
        return httpx.Response(
            200,
            stream=TrackingStream(b"data: " + data + b"\n\n"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings(), clock=lambda: 100.0)
        iterator = await adapter.chat_stream("question")
        events = [event async for event in iterator]

    assert auth_calls == 2
    assert chat_tokens == ["token-1", "token-2"]
    assert [event.text_fragment for event in events] == ["ok"]


async def test_chat_stream_does_not_retry_non_auth_http_failure() -> None:
    chat_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        chat_calls += 1
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        with pytest.raises(KiraHttpError) as error:
            await adapter.chat_stream("question")

    assert error.value.status_code == 503
    assert error.value.retryable
    assert chat_calls == 1


async def test_authentication_timeout_is_typed_and_sanitized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("contains no credential", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        with pytest.raises(KiraTimeoutError) as error:
            await adapter.authenticate()

    assert error.value.stage == "authentication"
    assert "basic-credential" not in repr(error.value)


async def test_stream_timeout_is_raised_after_prior_events_and_stream_is_closed() -> None:
    data = json.dumps(sse_payload("partial")).encode()
    stream_error = httpx.ReadTimeout("read timed out")
    stream = TrackingStream(b"data: " + data + b"\n\n", error=stream_error)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        stream_error.request = request
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        iterator = await adapter.chat_stream("question")
        first = await anext(iterator)
        assert first.text_fragment == "partial"
        with pytest.raises(KiraTimeoutError) as error:
            await anext(iterator)

    assert error.value.stage == "chat stream"
    assert stream.closed


async def test_malformed_sse_closes_stream() -> None:
    stream = TrackingStream(b"data: not-json\n\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        iterator = await adapter.chat_stream("question")
        with pytest.raises(KiraMalformedSseError):
            await anext(iterator)

    assert stream.closed


async def test_closing_iterator_cancels_downstream_stream() -> None:
    data = json.dumps(sse_payload("first")).encode()
    stream = TrackingStream(b"data: " + data + b"\n\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        iterator = await adapter.chat_stream("question")
        assert (await anext(iterator)).text_fragment == "first"
        await iterator.aclose()  # type: ignore[attr-defined]

    assert stream.closed


async def test_closing_iterator_before_first_event_closes_downstream_stream() -> None:
    data = json.dumps(sse_payload("never-read")).encode()
    stream = TrackingStream(b"data: " + data + b"\n\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KiraHttpAdapter(client, make_settings())
        iterator = await adapter.chat_stream("question")
        await iterator.aclose()  # type: ignore[attr-defined]

    assert stream.closed
