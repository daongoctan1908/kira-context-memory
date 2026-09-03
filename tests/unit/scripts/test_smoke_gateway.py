import io
import json

import httpx

from scripts.smoke_gateway import SmokeOptions, SseEvent, iter_sse_events, run_smoke


def options() -> SmokeOptions:
    return SmokeOptions(
        gateway_url="http://gateway.test",
        session_id="smoke-session",
        message="Hưng Yên thì sao?",
    )


def test_sse_parser_supports_comments_and_multiline_data() -> None:
    lines = [
        ": keep-alive",
        "event: custom",
        "data: first",
        "data: second",
        "",
        "data: final",
    ]

    assert list(iter_sse_events(lines)) == [
        SseEvent(name="custom", data="first\nsecond"),
        SseEvent(name="message", data="final"),
    ]


def test_smoke_streams_text_and_reports_correlation_summary() -> None:
    status = {
        "data": {
            "response": [
                {
                    "type": "status_response",
                    "content": {
                        "statusResponse": {
                            "name": "Phân tích yêu cầu",
                            "status": "completed",
                        }
                    },
                }
            ],
            "requestId": "request-1",
            "messageId": "message-1",
        }
    }
    text_one = {
        "data": {
            "response": [{"type": "text", "content": {"text": "Dạ "}}],
            "requestId": "request-1",
            "messageId": "message-1",
        }
    }
    text_two = {
        "data": {
            "response": [{"type": "text", "content": {"text": "đúng rồi"}}],
            "requestId": "request-1",
            "messageId": "message-1",
        }
    }
    body = "".join(
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        for payload in (status, text_one, text_two)
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://gateway.test/chat"
        assert json.loads(request.content) == {
            "session_id": "smoke-session",
            "message": "Hưng Yên thì sao?",
        }
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Correlation-ID": "correlation-1",
            },
            content=body,
            request=request,
        )

    stdout = io.StringIO()
    stderr = io.StringIO()

    result = run_smoke(
        options(),
        transport=httpx.MockTransport(handler),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert stdout.getvalue() == "Dạ đúng rồi\n"
    assert "STATUS Phân tích yêu cầu: completed" in stderr.getvalue()
    assert "PASS events=3 characters=11 correlation_id=correlation-1" in stderr.getvalue()
    assert "request_ids=request-1 message_ids=message-1" in stderr.getvalue()


def test_smoke_fails_on_gateway_error_event_without_printing_raw_payload() -> None:
    body = (
        b'event: gateway_error\ndata: {"code":"KIRA_TIMEOUT",'
        b'"message":"KiRa chat stream timed out","correlation_id":"correlation-2",'
        b'"retryable":true}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Correlation-ID": "correlation-2",
            },
            content=body,
            request=request,
        )

    stdout = io.StringIO()
    stderr = io.StringIO()

    result = run_smoke(
        options(),
        transport=httpx.MockTransport(handler),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 3
    assert stdout.getvalue() == ""
    assert stderr.getvalue().strip() == (
        "FAIL gateway_error code=KIRA_TIMEOUT correlation_id=correlation-2"
    )
    assert "timed out" not in stderr.getvalue()


def test_smoke_fails_when_stream_has_no_text() -> None:
    body = b'data: {"data":{"response":[]}}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=body,
            request=request,
        )

    result = run_smoke(
        options(),
        transport=httpx.MockTransport(handler),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert result == 2


def test_smoke_reports_sanitized_http_failure() -> None:
    body = json.dumps(
        {
            "code": "KIRA_TIMEOUT",
            "message": "sanitized",
            "correlation_id": "correlation-3",
            "retryable": True,
        }
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            504,
            headers={
                "Content-Type": "application/json",
                "X-Correlation-ID": "correlation-3",
            },
            stream=httpx.ByteStream(body),
            request=request,
        )

    stderr = io.StringIO()
    result = run_smoke(
        options(),
        transport=httpx.MockTransport(handler),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert result == 3
    assert stderr.getvalue().strip() == (
        "FAIL Gateway HTTP 504 code=KIRA_TIMEOUT correlation_id=correlation-3"
    )
    assert "sanitized" not in stderr.getvalue()
