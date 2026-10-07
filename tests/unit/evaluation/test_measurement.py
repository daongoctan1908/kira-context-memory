"""Measured calls stay distinct from source deliveries, skips and unavailable usage."""

import httpx
import pytest

from evaluation.formation import FormationCaptureObserver
from evaluation.measurement import (
    MeasurementRecorder,
    ProviderStage,
    http_measurement_hooks,
    measure_stage,
    measurement_arm,
    merge_measurements,
    record_provider_call,
    token_usage,
)
from evaluation.timing import TimingStage


async def test_http_hook_counts_request_and_preserves_response_without_capturing_content():
    payload = {
        "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
        "choices": [{"message": {"content": "sensitive content"}}],
    }
    recorder = MeasurementRecorder()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        event_hooks=http_measurement_hooks(ProviderStage.REWRITE),
    ) as client:
        # Calls outside the run are not counted as benchmark traffic.
        await client.post("https://test.invalid/chat", json={})
        with recorder.bind(), measurement_arm("with_ltm"):
            response = await client.post("https://test.invalid/chat", json={})
    assert response.json() == payload
    calls = recorder.snapshot().provider_calls
    assert len(calls) == 1
    assert calls[0].arm == "with_ltm"
    assert calls[0].usage.total_tokens == 23
    assert "sensitive content" not in recorder.snapshot().model_dump_json()


async def test_transport_failure_is_counted_without_invented_zero_usage():
    def failure(request):
        raise httpx.ConnectError("private connection detail", request=request)

    recorder = MeasurementRecorder()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(failure),
        event_hooks=http_measurement_hooks(ProviderStage.REWRITE),
    ) as client:
        with recorder.bind(), pytest.raises(httpx.ConnectError):
            await client.post("https://test.invalid/chat", json={})
    call = recorder.snapshot().provider_calls[0]
    assert call.outcome == "error"
    assert call.usage is None
    assert "private" not in recorder.snapshot().model_dump_json()


def test_source_measurement_is_not_charged_to_first_qa_and_resume_merges_once():
    qa, source = MeasurementRecorder(), MeasurementRecorder()
    observer = FormationCaptureObserver()
    with qa.bind():
        with source.bind(), measure_stage(TimingStage.FORMATION):
            with observer.observe("mem0.extract") as stage:
                stage.set_usage({"input": 10, "output": 2, "total": 12})
                stage.set_outcome("success")
            with observer.observe("mem0.memory.embed") as stage:
                stage.set_attribute("gen_ai.request.model", "embedding-model")
                stage.set_usage({"input": 4, "total": 4})
        with measurement_arm("no_ltm"), measure_stage(TimingStage.REWRITE):
            record_provider_call(ProviderStage.REWRITE, basis="http_request", outcome="success")
    assert [call.stage for call in source.snapshot().provider_calls] == [
        ProviderStage.EXTRACTION,
        ProviderStage.EMBEDDING,
    ]
    assert len(qa.snapshot().provider_calls) == 1
    assert set(qa.snapshot().timing) == {"no_ltm"}
    later = MeasurementRecorder()
    with later.bind(), measure_stage(TimingStage.FORMATION):
        record_provider_call(ProviderStage.EXTRACTION, basis="sdk_invocation", outcome="success")
    merged = merge_measurements(source.snapshot(), later.snapshot())
    assert len(merged.provider_calls) == 3
    assert len(merged.timing["unpaired"].attempts) == 2
    assert merged.timing["unpaired"].stages[0].attempts == 2


@pytest.mark.parametrize("value", [None, {}, {"total_tokens": True}, {"input": -1}, "usage"])
def test_missing_or_invalid_usage_stays_unknown(value):
    assert token_usage(value) is None


def test_provider_omits_output_tokens_no_inference_from_total():
    usage = token_usage({"input": 8, "total": 10})
    assert usage.prompt_tokens == 8
    assert usage.completion_tokens is None
    assert usage.total_tokens == 10
