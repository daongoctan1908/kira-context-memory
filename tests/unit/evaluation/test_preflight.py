"""Protocol, isolation and failure-path coverage without external provider requests."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from evaluation.config import EvalConfig, ProviderConfig, load_config
from evaluation.mock import mock_response
from evaluation.models import Outcome, Probe, Profile, Reason, Suite
from evaluation.preflight import run_preflight
from evaluation.providers import api_url, embedding_observations, extraction_count


def configured(suites=(Suite.FORMATION,), **overrides):
    provider = ProviderConfig(
        base_url="https://provider.test/v1",
        model="test-model",
        api_key=SecretStr("sk-synthetic-key"),
        auth_required=True,
    )
    return EvalConfig(
        profile=Profile.EXTERNAL_SYNTHETIC,
        suites=suites,
        extraction=provider,
        rewrite=provider,
        embedding=provider,
        **overrides,
    )


def chat_response(
    content='{"memory":[{"id":"0","text":"User prefers tables.","attributed_to":"user"}]}',
    **choice_overrides,
):
    choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
    choice.update(choice_overrides)
    return {
        "model": "model-snapshot",
        "choices": [choice],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


async def test_rewrite_only_does_not_require_extraction_embedding_or_db():
    calls = []

    def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["stream"] is False and body["temperature"] == 0
        assert body["max_tokens"] == 256 and "response_format" not in body
        assert request.headers["Authorization"] == "Bearer sk-synthetic-key"
        return httpx.Response(200, json=chat_response("Một truy vấn độc lập."))

    report = await run_preflight(
        configured((Suite.REWRITE,)), transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 1
    assert [c.probe for c in report.checks] == [Probe.REWRITE_CHAT]
    assert report.suites[0].outcome == Outcome.PASS
    assert report.checks[0].returned_model == "model-snapshot"
    assert report.checks[0].usage.total_tokens == 30
    assert "sk-synthetic-key" not in report.model_dump_json()
    assert "Một truy vấn độc lập" not in report.model_dump_json()


async def test_json_contract_and_shared_dependencies_run_once():
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["max_tokens"] == 1000
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json=chat_response())

    report = await run_preflight(
        configured((Suite.FORMATION, Suite.FORMATION)), transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 1
    assert report.checks[0].extracted_fact_count == 1
    assert report.suites[0].outcome == Outcome.PASS


async def test_prompt_only_mode_does_not_silently_send_json_mode():
    def handle(request):
        assert "response_format" not in json.loads(request.content)
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=chat_response())

    provider = ProviderConfig(base_url="http://internal.test", model="qwen-test")
    config = EvalConfig(
        profile=Profile.INTERNAL_TEST, extraction=provider, extraction_json_mode="prompt_only"
    )
    report = await run_preflight(config, transport=httpx.MockTransport(handle))
    assert report.checks[0].outcome == Outcome.PASS


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, Reason.AUTH),
        (403, Reason.AUTH),
        (429, Reason.RATE_LIMIT),
        (500, Reason.HTTP_STATUS),
        (400, Reason.HTTP_STATUS),
        (302, Reason.HTTP_STATUS),
    ],
)
async def test_http_errors_do_not_retry_follow_redirects_or_leak_body(status, reason):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="sk-synthetic-key sensitive-error",
            headers={"Location": "https://credential-collector.test"},
        )

    report = await run_preflight(configured(), transport=httpx.MockTransport(handle))
    assert len(calls) == 1
    assert report.checks[0].reason == reason
    assert report.checks[0].http_status == status
    assert report.suites[0].outcome == Outcome.DEPENDENCY_ERROR
    assert "sensitive-error" not in report.model_dump_json()
    assert "sk-synthetic-key" not in report.model_dump_json()


@pytest.mark.parametrize(
    "error,reason", [(httpx.ConnectError, Reason.CONNECTION), (httpx.ReadTimeout, Reason.TIMEOUT)]
)
async def test_transport_failure_mapping(error, reason):
    def handle(request):
        raise error("sk-synthetic-key", request=request)

    report = await run_preflight(configured(), transport=httpx.MockTransport(handle))
    assert report.checks[0].reason == reason
    assert "sk-synthetic-key" not in report.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"choices": []},
        chat_response(""),
        chat_response("   "),
        chat_response("text", finish_reason="length"),
        chat_response("text", message={"role": "assistant", "content": "text", "tool_calls": [{}]}),
        chat_response(
            "text", message={"role": "assistant", "content": "text", "refusal": "refused"}
        ),
        chat_response("text", message={"role": "user", "content": "text"}),
        chat_response(123),
        chat_response("x" * 16385),
    ],
)
async def test_malformed_chat_is_protocol_error(body):
    report = await run_preflight(
        configured(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    assert report.checks[0].outcome == Outcome.PROTOCOL_ERROR


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "```json\n{}\n```",
        "[]",
        '{"facts":[]}',
        '{"memory":[]}',
        '{"memory":["text"]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"admin"}]}',
        '{"memory":[{"id":"2","text":"x","attributed_to":"user"}]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"user","linked_memory_ids":1}]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"user","linked_memory_ids":[1]}]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"user","taxonomy":"x"}]}',
    ],
)
async def test_invalid_extraction_is_not_a_passing_negative(content):
    report = await run_preflight(
        configured(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=chat_response(content))),
    )
    assert report.checks[0].outcome == Outcome.PROTOCOL_ERROR


async def test_response_byte_limit_and_invalid_json():
    for body, reason in [
        (b"x" * 1025, Reason.RESPONSE_TOO_LARGE),
        (b"{bad", Reason.INVALID_JSON),
        (b'{"x":NaN}', Reason.INVALID_JSON),
    ]:
        report = await run_preflight(
            configured(max_response_bytes=1024),
            transport=httpx.MockTransport(lambda _, body=body: httpx.Response(200, content=body)),
        )
        assert report.checks[0].reason == reason


def vector_body(vectors=None):
    return {
        "model": "embed-snapshot",
        "data": vectors
        if vectors is not None
        else [
            {"index": 1, "embedding": [0.1, 0.2, 0.3]},
            {"index": 0, "embedding": [0.3, 0.2, 0.1]},
        ],
    }


async def test_embedding_batch_discovery_and_missing_database_are_separate():
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert len(body["input"]) == 2 and body["encoding_format"] == "float"
        assert "dimensions" not in body
        return httpx.Response(200, json=vector_body())

    report = await run_preflight(
        configured((Suite.RETRIEVAL,)), transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 1
    assert report.checks[0].embedding_count == 2
    assert report.checks[0].embedding_dimension == 3
    assert report.checks[1].outcome == Outcome.NOT_RUN
    assert report.suites[0].outcome == Outcome.NOT_RUN


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [{"index": 0, "embedding": [0.1]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}],
        [{"index": True, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 2, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 1, "embedding": [0.1, 0.2]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 1, "embedding": []}, {"index": 0, "embedding": []}],
        [{"index": 1, "embedding": [True]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 1, "embedding": [float("inf")]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 1, "embedding": ["0.1"]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 1, "embedding": [0.0]}, {"index": 0, "embedding": [0.2]}],
        [None, None],
    ],
)
def test_invalid_embedding_rejected(bad):
    from evaluation.errors import ProtocolError

    with pytest.raises(ProtocolError):
        embedding_observations(vector_body(bad), None)


async def test_explicit_dimension_request_and_mismatch():
    def handle(request):
        assert json.loads(request.content)["dimensions"] == 4
        return httpx.Response(200, json=vector_body())

    report = await run_preflight(
        configured((Suite.RETRIEVAL,), embedding_dimensions=4),
        transport=httpx.MockTransport(handle),
    )
    assert report.checks[0].reason == Reason.DIMENSION_MISMATCH


async def test_timeout_cancellation_and_continuing_other_probes():
    async def handle(request):
        if request.url.path.endswith("chat/completions"):
            await asyncio.sleep(10)
        return httpx.Response(200, json=vector_body())

    report = await run_preflight(
        configured((Suite.FORMATION, Suite.RETRIEVAL), total_timeout_seconds=0.01),
        transport=httpx.MockTransport(handle),
    )
    assert report.checks[0].reason == Reason.TIMEOUT
    assert report.checks[1].embedding_dimension == 3


async def test_cancelled_caller_is_not_swallowed():
    async def handle(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_preflight(configured(), transport=httpx.MockTransport(handle))


async def test_missing_config_never_makes_requests():
    def fail(_):
        pytest.fail("No request expected")

    report = await run_preflight(
        EvalConfig(profile=Profile.INTERNAL_TEST, suites=(Suite.CROSS_SESSION,)),
        transport=httpx.MockTransport(fail),
    )
    assert all(c.outcome == Outcome.NOT_RUN and not c.attempted for c in report.checks)


async def test_persistent_formation_requires_queue_and_memory_schema_and_reuses_dimension():
    calls = []

    async def database(config, probe, dimension):
        calls.append((probe, dimension))
        return {}

    config = configured(
        formation_mode="persistent",
        database_url="postgresql://local/eval",
        memory_database_url="postgresql://local/eval",
    )
    report = await run_preflight(
        config, transport=httpx.MockTransport(mock_response), database_probe=database
    )
    assert report.suites[0].outcome == Outcome.PASS
    assert [p for p, _ in calls] == [Probe.PGVECTOR, Probe.MEMORY_SCHEMA, Probe.CONVERSATION_DB]
    assert all(d == 3 for _, d in calls)


async def test_memory_schema_waits_for_embedding_dimension():
    async def database(config, probe, dimension):
        assert probe == Probe.PGVECTOR
        return {}

    config = configured((Suite.RETRIEVAL,), memory_database_url="postgresql://local/eval")
    report = await run_preflight(
        config,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        database_probe=database,
    )
    assert report.checks[2].reason == Reason.PREREQUISITE_FAILED


async def test_all_mock_suites_are_simulated_and_do_not_use_real_transports():
    config = load_config(profile=Profile.MOCK, suites=tuple(Suite))
    report = await run_preflight(config)
    assert report.simulated
    assert all(c.outcome == Outcome.PASS for c in report.checks)
    assert len(report.checks) == len(Probe)


@pytest.mark.parametrize("path", ["/ready", "/_test/requests"])
async def test_health_payload_identity_is_validated(path):
    def handle(request):
        if request.url.path == path:
            return httpx.Response(200, json={"status": "ok", "stub": "unrelated"})
        return mock_response(request)

    config = load_config(profile=Profile.MOCK, suites=(Suite.CROSS_SESSION,))
    report = await run_preflight(config, transport=httpx.MockTransport(handle))
    assert report.suites[0].outcome == Outcome.PROTOCOL_ERROR
    assert any(c.reason == Reason.INVALID_HEALTH for c in report.checks)


def test_endpoint_normalization_and_dual_source_envelope():
    assert api_url("https://model.test/v1/", "/embeddings") == "https://model.test/v1/embeddings"
    assert api_url("https://model.test", "/embeddings") == "https://model.test/v1/embeddings"
    assert (
        extraction_count(
            '{"memory":[{"id":"0","text":"Assistant suggestion",'
            '"attributed_to":"assistant","linked_memory_ids":[]}]}'
        )
        == 1
    )


@pytest.mark.parametrize("usage", ["invalid", {"prompt_tokens": -1}, {"total_tokens": True}])
async def test_invalid_usage_is_protocol_error_without_echoing_payload(usage):
    body = chat_response()
    body["usage"] = usage
    report = await run_preflight(
        configured(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    assert report.checks[0].reason == Reason.INVALID_JSON


async def test_reflected_credential_in_model_is_not_reported():
    body = chat_response()
    body["model"] = "sk-synthetic-key"
    report = await run_preflight(
        configured(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    assert report.checks[0].returned_model is None
    assert "sk-synthetic-key" not in report.model_dump_json()


@pytest.mark.parametrize("suite", [Suite.FORMATION, Suite.RETRIEVAL])
async def test_arbitrary_provider_credential_reflection_is_redacted(suite):
    provider = ProviderConfig(
        base_url="https://provider.test/v1",
        model="test-model",
        api_key=SecretStr("synthetic-secret-token"),
    )
    config = EvalConfig(
        profile=Profile.INTERNAL_TEST, suites=(suite,), extraction=provider, embedding=provider
    )
    body = chat_response() if suite == Suite.FORMATION else vector_body()
    body["model"] = "prefix-synthetic-secret-token-suffix"
    report = await run_preflight(
        config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    assert report.checks[0].returned_model is None
    assert "synthetic-secret-token" not in report.model_dump_json()
