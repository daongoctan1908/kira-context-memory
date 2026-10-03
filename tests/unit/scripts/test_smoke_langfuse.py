from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from scripts.local import smoke_langfuse
from scripts.local.smoke_product_stack import ProductSmokeOptions, ProductSmokeResult


@pytest.mark.parametrize("capture", [False, True])
def test_cli_can_verify_timing_only_or_content_capture(capture: bool) -> None:
    options = smoke_langfuse.parse_args(
        ["--capture-content" if capture else "--no-capture-content", "--timeout", "5"]
    )
    assert options.capture_content is capture
    assert options.product.timeout_seconds == 5


def test_timing_only_acceptance_rejects_any_captured_content() -> None:
    trace = {"observations": [{"input": None, "output": "must-not-be-captured"}]}
    with pytest.raises(smoke_langfuse.LangfuseAcceptanceError):
        smoke_langfuse._assert_content_absent([trace])


async def test_recent_traces_only_accept_jobs_created_by_this_smoke() -> None:
    started_at = datetime(2026, 10, 2, tzinfo=UTC)
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        if request.url.path == "/api/public/observations":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"traceId": trace_id, "startTime": started_at.isoformat()}
                        for trace_id in ("ours", "concurrent", "ours")
                    ]
                },
            )
        trace_id = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            json={
                "id": trace_id,
                "metadata": {"event_id": "our-job" if trace_id == "ours" else "other-job"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        traces = await smoke_langfuse._recent_traces(
            client,
            "http://langfuse.test",
            observation_name="chat.request",
            started_at=started_at,
            expected_event_ids=frozenset({"our-job"}),
        )

    assert [trace["id"] for trace in traces] == ["ours"]
    assert requested[0].url.params["fromStartTime"] == started_at.isoformat()
    assert requested[0].url.params["limit"] == "100"
    assert len(requested) == 3


async def test_acceptance_passes_exact_product_job_ids_to_trace_polling(monkeypatch) -> None:
    event_ids = (UUID(int=1), UUID(int=2))
    observed_ids: list[frozenset[str]] = []
    metadata = {
        "event_id": str(event_ids[0]),
        "correlation_id": "correlation",
        "turn_id": "turn",
        "origin_trace_id": "chat-trace",
    }
    chat = {"id": "chat-trace", "metadata": metadata}
    worker = {"id": "worker-trace", "metadata": metadata}

    async def product_smoke(options):
        return ProductSmokeResult(event_ids)

    async def wait_for_traces(client, options, *, started_at, expected_event_ids):
        observed_ids.append(expected_event_ids)
        return [chat], [worker]

    monkeypatch.setattr(smoke_langfuse, "run_product_smoke", product_smoke)
    monkeypatch.setattr(smoke_langfuse, "_wait_for_traces", wait_for_traces)
    monkeypatch.setattr(
        smoke_langfuse, "_validate_traces", lambda chats, workers, **kwargs: (chat, worker)
    )
    await smoke_langfuse.run(
        smoke_langfuse.LangfuseAcceptanceOptions(
            product=ProductSmokeOptions(),
            langfuse_url="http://langfuse.test",
            public_key="synthetic-public",
            secret_key="synthetic-secret",
            timeout_seconds=1,
        )
    )
    assert observed_ids == [frozenset(str(value) for value in event_ids)]
