import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.config.settings import Settings
from app.domain.errors.kira import KiraHttpError, KiraTimeoutError
from app.domain.models.kira import KiraAuthResult, KiraEventKind, KiraStreamEvent
from app.presentation.api.main import create_app


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        kira_base_url="http://kira.test:8122",
        kira_username="service-account",
        kira_basic_auth="basic-credential",
    )


class ListStream(AsyncIterator[KiraStreamEvent]):
    def __init__(self, events: list[KiraStreamEvent], error: Exception | None = None) -> None:
        self._events = iter(events)
        self._error = error
        self._raised = False
        self.closed = False

    def __aiter__(self) -> "ListStream":
        return self

    async def __anext__(self) -> KiraStreamEvent:
        try:
            return next(self._events)
        except StopIteration:
            if self._error is not None and not self._raised:
                self._raised = True
                raise self._error from None
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class FakeKiraClient:
    def __init__(
        self,
        *,
        events: list[KiraStreamEvent] | None = None,
        open_error: Exception | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.events = events or []
        self.open_error = open_error
        self.stream_error = stream_error
        self.messages: list[str] = []
        self.last_stream: ListStream | None = None

    async def authenticate(self) -> KiraAuthResult:
        return KiraAuthResult(token="unused")

    async def chat_stream(self, message: str) -> AsyncIterator[KiraStreamEvent]:
        self.messages.append(message)
        if self.open_error is not None:
            raise self.open_error
        self.last_stream = ListStream(self.events, self.stream_error)
        return self.last_stream


def kira_event(raw_data: str, text: str | None = None) -> KiraStreamEvent:
    return KiraStreamEvent(
        kind=KiraEventKind.TEXT if text is not None else KiraEventKind.STATUS,
        raw_data=raw_data,
        payload=json.loads(raw_data),
        text_fragment=text,
        request_id="request-1",
        message_id="message-1",
    )


@asynccontextmanager
async def gateway_client(kira_client: FakeKiraClient) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=make_settings(), kira_client=kira_client)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            yield client


async def test_health_and_readiness_do_not_probe_kira() -> None:
    kira_client = FakeKiraClient()

    async with gateway_client(kira_client) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert kira_client.messages == []


async def test_ready_is_503_before_lifespan_initialization() -> None:
    app = create_app(settings=make_settings(), kira_client=FakeKiraClient())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


async def test_chat_proxies_raw_kira_frames_and_sets_stream_headers() -> None:
    status_data = json.dumps({"data": {"response": [{"type": "status_response"}]}})
    text_data = json.dumps({"data": {"response": [{"type": "text"}]}})
    kira_client = FakeKiraClient(events=[kira_event(status_data), kira_event(text_data, "answer")])

    async with gateway_client(kira_client) as client:
        response = await client.post(
            "/chat",
            json={"session_id": "session-1", "message": "question"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-correlation-id"]
    assert response.content == f"data: {status_data}\n\ndata: {text_data}\n\n".encode()
    assert kira_client.messages == ["question"]
    assert kira_client.last_stream is not None and kira_client.last_stream.closed


async def test_chat_rejects_client_supplied_user_id() -> None:
    async with gateway_client(FakeKiraClient()) as client:
        response = await client.post(
            "/chat",
            json={
                "session_id": "session-1",
                "message": "question",
                "user_id": "untrusted-user",
            },
        )

    assert response.status_code == 422


async def test_pre_stream_timeout_maps_to_504_json() -> None:
    kira_client = FakeKiraClient(open_error=KiraTimeoutError(stage="chat connection"))

    async with gateway_client(kira_client) as client:
        response = await client.post(
            "/chat",
            json={"session_id": "session-1", "message": "question"},
        )

    assert response.status_code == 504
    assert response.headers["x-correlation-id"]
    assert response.json() == {
        "code": "KIRA_TIMEOUT",
        "message": "KiRa chat connection timed out",
        "correlation_id": response.headers["x-correlation-id"],
        "retryable": True,
    }


async def test_pre_stream_http_failure_maps_to_sanitized_502_json() -> None:
    kira_client = FakeKiraClient(open_error=KiraHttpError(status_code=503))

    async with gateway_client(kira_client) as client:
        response = await client.post(
            "/chat",
            json={"session_id": "session-1", "message": "question"},
        )

    assert response.status_code == 502
    assert response.json() == {
        "code": "KIRA_HTTP_ERROR",
        "message": "KiRa returned HTTP 503",
        "correlation_id": response.headers["x-correlation-id"],
        "retryable": True,
    }


async def test_midstream_timeout_emits_gateway_error_and_closes_stream() -> None:
    raw_data = json.dumps({"data": {"response": [{"type": "text"}]}})
    kira_client = FakeKiraClient(
        events=[kira_event(raw_data, "partial")],
        stream_error=KiraTimeoutError(stage="chat stream"),
    )

    async with gateway_client(kira_client) as client:
        response = await client.post(
            "/chat",
            json={"session_id": "session-1", "message": "question"},
        )

    assert response.status_code == 200
    assert response.content.startswith(f"data: {raw_data}\n\n".encode())
    assert b"event: gateway_error\n" in response.content
    assert b'"code":"KIRA_TIMEOUT"' in response.content
    error_data = response.content.split(b"event: gateway_error\ndata: ", maxsplit=1)[1]
    error_payload = json.loads(error_data)
    assert error_payload["correlation_id"] == response.headers["x-correlation-id"]
    assert kira_client.last_stream is not None and kira_client.last_stream.closed
