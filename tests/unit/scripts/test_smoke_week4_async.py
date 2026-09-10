import json

import httpx
import pytest

from scripts.smoke_week4_async import (
    EXPECTED_KIRA_TEXT,
    Week4SmokeError,
    _chat,
    _metric_value,
    parse_args,
)


@pytest.mark.asyncio
async def test_chat_collects_text_from_sse_without_exposing_non_text_frames() -> None:
    body = "".join(
        (
            'data: {"data":{"response":[{"type":"status_response",'
            '"content":{"status":"running"}}]}}\n\n',
            "data: "
            + json.dumps(
                {"data": {"response": [{"type": "text", "content": {"text": "Mock KiRa "}}]}}
            )
            + "\n\n",
            "data: "
            + json.dumps({"data": {"response": [{"type": "text", "content": {"text": "answer"}}]}})
            + "\n\n",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "session_id": "session-a",
            "message": "question",
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=body,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await _chat(client, "http://gateway.test", "session-a", "question")

    assert answer == EXPECTED_KIRA_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (503, "application/json", "{}"),
        (200, "text/event-stream", "event: gateway_error\ndata: {}\n\n"),
        (200, "text/event-stream", 'data: {"data":{"response":[]}}\n\n'),
    ],
)
async def test_chat_rejects_non_sse_gateway_error_and_empty_text(
    status: int,
    content_type: str,
    body: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"Content-Type": content_type},
            content=body,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Week4SmokeError):
            await _chat(client, "http://gateway.test", "session-a", "question")


def test_metric_value_requires_one_exact_numeric_series() -> None:
    payload = (
        '# HELP metric example\nmetric{outcome="success"} 2.0\nmetric{outcome="failure"} 9.0\n'
    )

    assert _metric_value(payload, 'metric{outcome="success"}') == 2.0

    with pytest.raises(Week4SmokeError):
        _metric_value(payload, 'metric{outcome="missing"}')
    with pytest.raises(Week4SmokeError):
        _metric_value("metric 1\nmetric 2\n", "metric")
    with pytest.raises(Week4SmokeError):
        _metric_value("metric not-a-number\n", "metric")


def test_parse_args_normalizes_urls_and_accepts_database_override() -> None:
    options = parse_args(
        [
            "--gateway-url",
            "http://gateway.test/",
            "--database-url",
            "postgresql+asyncpg://test.invalid/db",
            "--timeout",
            "3.5",
        ]
    )

    assert options.gateway_url == "http://gateway.test"
    assert options.database_url == "postgresql+asyncpg://test.invalid/db"
    assert options.timeout_seconds == 3.5


def test_parse_args_rejects_non_positive_timeout() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--timeout", "0"])
