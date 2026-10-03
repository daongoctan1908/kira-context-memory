import json
from collections.abc import AsyncIterator

import anyio
import anyio.lowlevel
import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config.settings import Settings
from app.domain.errors.kira import (
    KiraAuthenticationError,
    KiraHttpError,
    KiraMalformedSseError,
    KiraTimeoutError,
)
from app.domain.models.kira import KiraEventKind
from app.infrastructure.kira.http_kira_client import KiraHttpAdapter
from app.infrastructure.observability.tracing import bind_content_capture


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


class RecordingStreamObserver:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def kira_stream_observed(
        self,
        outcome: str,
        seconds: float,
        *,
        first_event_seconds: float | None,
        first_content_seconds: float | None,
    ) -> None:
        self.calls.append(
            {
                "outcome": outcome,
                "seconds": seconds,
                "first_event_seconds": first_event_seconds,
                "first_content_seconds": first_content_seconds,
            }
        )


class FailingStreamObserver:
    def kira_stream_observed(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("private metric failure")


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
    metric_observer = RecordingStreamObserver()

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
        adapter = KiraHttpAdapter(client, make_settings(), metric_observer=metric_observer)
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
    assert len(metric_observer.calls) == 1
    assert metric_observer.calls[0]["outcome"] == "success"
    assert metric_observer.calls[0]["seconds"] >= 0
    assert metric_observer.calls[0]["first_event_seconds"] >= 0
    assert metric_observer.calls[0]["first_content_seconds"] >= 0


async def test_chat_trace_measures_stream_milestones_with_masked_bounded_content() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.kira")
    data = json.dumps(sse_payload("call +84 912 345 678")).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(
            200,
            stream=TrackingStream(b"data: " + data + b"\n\n"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with bind_content_capture(True), tracer.start_as_current_span("chat.request") as root:
            iterator = await KiraHttpAdapter(
                client,
                make_settings(),
                tracer=tracer,
            ).chat_stream("email user@example.com")
            assert [event.text_fragment async for event in iterator] == ["call +84 912 345 678"]

    spans = exporter.get_finished_spans()
    auth_span = next(span for span in spans if span.name == "kira.authenticate")
    chat_span = next(span for span in spans if span.name == "kira.chat")
    assert auth_span.parent is not None and auth_span.parent.span_id == root.context.span_id
    assert chat_span.parent is not None and chat_span.parent.span_id == root.context.span_id
    assert auth_span.attributes is not None
    assert auth_span.attributes["kira.auth.cache_status"] == "miss"
    assert auth_span.attributes["kira.outcome"] == "success"
    assert chat_span.attributes is not None
    assert chat_span.attributes["kira.outcome"] == "success"
    assert chat_span.attributes["kira.request_id"] == "request-1"
    assert chat_span.attributes["kira.message_id"] == "message-1"
    assert chat_span.attributes["kira.stream.first_event_seconds"] >= 0
    assert chat_span.attributes["kira.stream.first_content_seconds"] >= 0
    assert "user@example.com" not in str(chat_span.attributes)
    assert "+84 912 345 678" not in str(chat_span.attributes)
    assert "[REDACTED_EMAIL]" in chat_span.attributes["langfuse.observation.input"]
    assert "[REDACTED_PHONE]" in chat_span.attributes["langfuse.observation.output"]
    provider.shutdown()


async def test_disabled_content_capture_does_not_mask_or_buffer_stream_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.kira")
    data = json.dumps(sse_payload("private-stream-content")).encode()

    def unexpected_masking(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("disabled content must not be processed for telemetry")

    monkeypatch.setattr(
        "app.infrastructure.kira.http_kira_client.masked_io_attributes", unexpected_masking
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(
            200,
            stream=TrackingStream(b"data: " + data + b"\n\n"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with bind_content_capture(False):
            iterator = await KiraHttpAdapter(client, make_settings(), tracer=tracer).chat_stream(
                "private-input"
            )
            assert (await anext(iterator)).text_fragment == "private-stream-content"
            assert iterator._output_fragments == []
            assert [event async for event in iterator] == []

    chat_span = next(span for span in exporter.get_finished_spans() if span.name == "kira.chat")
    assert chat_span.attributes is not None
    assert "langfuse.observation.input" not in chat_span.attributes
    assert "langfuse.observation.output" not in chat_span.attributes
    assert chat_span.attributes["kira.stream.first_content_seconds"] >= 0
    assert chat_span.attributes["kira.outcome"] == "success"
    provider.shutdown()


@pytest.mark.parametrize(
    ("content", "expected_truncated"),
    [
        ("-----BEGIN RSA PRIVATE KEY-----\n" + "PRIVATEKEYBODY" * 500, True),
        ("ế" * 2000, False),
    ],
    ids=["private-key-prefix", "unicode-bytes-within-character-limit"],
)
async def test_stream_capture_masks_key_prefix_and_reports_actual_truncation(
    content: str, expected_truncated: bool
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    data = json.dumps(sse_payload(content), ensure_ascii=False).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(
            200,
            stream=TrackingStream(b"data: " + data + b"\n\n"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with bind_content_capture(True):
            iterator = await KiraHttpAdapter(
                client, make_settings(), tracer=provider.get_tracer("test.kira")
            ).chat_stream('{"password":"private-input"}')
            assert [event.text_fragment async for event in iterator] == [content]
            assert iterator._output_fragments == []

    chat_span = next(span for span in exporter.get_finished_spans() if span.name == "kira.chat")
    assert chat_span.attributes is not None
    attributes = chat_span.attributes
    rendered = attributes["langfuse.observation.output"]
    assert len(rendered) <= 4096
    assert isinstance(json.loads(rendered), str)
    assert "PRIVATEKEYBODY" not in rendered
    assert "private-input" not in attributes["langfuse.observation.input"]
    assert attributes["kira.observation.output.truncated"] is expected_truncated
    assert attributes["kira.observation.output.original_bytes"] == len(content.encode("utf-8"))
    provider.shutdown()


async def test_invalid_unicode_telemetry_does_not_interrupt_stream_events() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    content = "\ud800"
    data = json.dumps(sse_payload(content)).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(
            200,
            stream=TrackingStream(b"data: " + data + b"\n\n"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with bind_content_capture(True):
            iterator = await KiraHttpAdapter(
                client, make_settings(), tracer=provider.get_tracer("test.kira")
            ).chat_stream("question")
            assert [event.text_fragment async for event in iterator] == [content]
            assert iterator._output_fragments == []

    chat_span = next(span for span in exporter.get_finished_spans() if span.name == "kira.chat")
    assert chat_span.attributes is not None
    assert "langfuse.observation.output" not in chat_span.attributes
    assert chat_span.attributes["kira.observation.output.content_omitted"] == "masking_error"
    assert chat_span.attributes["kira.outcome"] == "success"
    provider.shutdown()


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


async def test_stream_metric_failure_does_not_change_provider_calls_or_events() -> None:
    calls: list[str] = []
    data = json.dumps(sse_payload("ok")).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(
            200,
            stream=TrackingStream(b"data: " + data + b"\n\n"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        iterator = await KiraHttpAdapter(
            client,
            make_settings(),
            metric_observer=FailingStreamObserver(),
        ).chat_stream("question")
        events = [event async for event in iterator]

    assert calls == ["/authenticate", "/api/v1/chat"]
    assert [event.text_fragment for event in events] == ["ok"]


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


async def test_cancel_scope_does_not_interrupt_http_connection_cleanup():
    class CheckpointCloseStream(TrackingStream):
        async def aclose(self):
            await anyio.lowlevel.checkpoint()
            self.closed = True

    stream = CheckpointCloseStream(b"data: {}\n\n")

    async def handler(request):
        if request.url.path == "/authenticate":
            return httpx.Response(200, json=auth_response(), request=request)
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        iterator = await KiraHttpAdapter(client, make_settings()).chat_stream("question")
        with anyio.CancelScope() as scope:
            scope.cancel()
            await iterator.aclose()
        assert stream.closed
