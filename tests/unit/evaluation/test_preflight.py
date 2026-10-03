"""Protocol, isolation and failure-path coverage without external provider requests."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from evaluation.config import EvalConfig, ProviderConfig, load_config
from evaluation.mock import mock_database, mock_response
from evaluation.models import Outcome, Probe, Profile, Reason, Suite
from evaluation.preflight import run_preflight
from evaluation.providers import (
    api_url,
    embedding_observations,
    extraction_count,
    judge_observations,
    safe_model,
)


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
    content='{"memory":[{"id":"0","text":"User prefers tables.",'
    '"attributed_to":"user","scope":"GLOBAL"}]}',
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


async def test_path_like_served_model_is_preserved_in_request_and_preflight_observations():
    served_model = "/models/Qwen3_14B"
    provider = ProviderConfig(base_url="http://qwen.internal/v1", model=served_model)
    config = EvalConfig(
        profile=Profile.INTERNAL_TEST,
        suites=(Suite.REWRITE,),
        rewrite=provider,
    )

    def handle(request):
        assert json.loads(request.content)["model"] == served_model
        assert "authorization" not in request.headers
        body = chat_response("Một truy vấn độc lập.")
        body["model"] = served_model
        return httpx.Response(200, json=body)

    report = await run_preflight(config, transport=httpx.MockTransport(handle))

    check = next(check for check in report.checks if check.probe is Probe.REWRITE_CHAT)
    assert check.outcome is Outcome.PASS
    assert check.requested_model == check.returned_model == served_model


@pytest.mark.parametrize(
    "value",
    [None, 3, "", " bad", "bad ", "/models/bad\n", "/models/bad\t", "/models/\x00bad", "a" * 201],
)
def test_model_observation_sanitizer_keeps_existing_whitespace_control_and_length_guards(value):
    assert safe_model(value) is None


async def test_pc_rewrite_preflight_requires_and_validates_explicit_judge_schema():
    provider = ProviderConfig(
        base_url="https://provider.test/v1",
        model="explicit-model",
        api_key=SecretStr("synthetic-secret-token"),
        auth_required=True,
    )
    config = EvalConfig(
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        suites=(Suite.REWRITE,),
        rewrite=provider,
        judge=provider,
    )
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        schema = body.get("response_format", {}).get("json_schema", {})
        if schema.get("name") == "week5_judge_preflight":
            assert schema["strict"] is True
            assert schema["schema"]["properties"]["verdict"]["enum"] == [
                "PASS",
                "FAIL",
                "UNCERTAIN",
            ]
            return httpx.Response(
                200,
                json=chat_response('{"verdict":"PASS","reason_code":"synthetic_match"}'),
            )
        return httpx.Response(200, json=chat_response("Một truy vấn độc lập."))

    report = await run_preflight(config, transport=httpx.MockTransport(handle))

    assert len(calls) == 2
    assert [item.probe for item in report.checks] == [
        Probe.REWRITE_CHAT,
        Probe.JUDGE_SEMANTIC,
    ]
    assert report.checks[1].judge_verdict == "PASS"
    assert report.suites[0].outcome is Outcome.PASS
    assert "synthetic-secret-token" not in report.model_dump_json()


async def test_pc_semantic_suite_without_judge_is_not_ready():
    provider = ProviderConfig(base_url="https://provider.test/v1", model="explicit-model")
    config = EvalConfig(
        profile=Profile.PC_OPENAI_ACCEPTANCE,
        suites=(Suite.REWRITE,),
        rewrite=provider,
    )

    report = await run_preflight(config, transport=httpx.MockTransport(mock_response))

    assert report.checks[-1].probe is Probe.JUDGE_SEMANTIC
    assert report.checks[-1].outcome is Outcome.NOT_RUN
    assert report.suites[0].outcome is Outcome.NOT_RUN


async def test_json_contract_and_shared_dependencies_run_once():
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["max_tokens"] == 1000
        assert body["response_format"] == {"type": "json_object"}
        assert '"scope":"GLOBAL"' in body["messages"][0]["content"]
        assert "scope equal to CONVERSATION or GLOBAL" in body["messages"][0]["content"]
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
        '{"memory":[{"id":"0","text":"x","attributed_to":"admin","scope":"GLOBAL"}]}',
        '{"memory":[{"id":"2","text":"x","attributed_to":"user","scope":"GLOBAL"}]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"user",'
        '"scope":"GLOBAL","linked_memory_ids":1}]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"user",'
        '"scope":"GLOBAL","linked_memory_ids":[1]}]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"user","scope":"GLOBAL","taxonomy":"x"}]}',
        '{"memory":[{"id":"0","text":"x","attributed_to":"user"}]}',
    ],
)
async def test_invalid_extraction_is_not_a_passing_negative(content):
    report = await run_preflight(
        configured(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=chat_response(content))),
    )
    assert report.checks[0].outcome == Outcome.PROTOCOL_ERROR


@pytest.mark.parametrize("scope", [None, "", "INVALID", 1, False, [], {}])
async def test_extraction_probe_requires_valid_explicit_scope(scope):
    content = json.dumps(
        {"memory": [{"id": "0", "text": "fact", "attributed_to": "user", "scope": scope}]}
    )
    report = await run_preflight(
        configured(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=chat_response(content))),
    )

    assert report.checks[0].outcome == Outcome.PROTOCOL_ERROR


@pytest.mark.parametrize("scope", ["GLOBAL", "CONVERSATION", " global "])
def test_extraction_probe_accepts_native_scope_normalization(scope):
    content = json.dumps(
        {"memory": [{"id": "0", "text": "fact", "attributed_to": "user", "scope": scope}]}
    )

    assert extraction_count(content) == 1


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
    assert len(report.checks) == len(Probe) - 1  # Real KiRa is separate from the mock identity.


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
            '"attributed_to":"assistant","scope":"CONVERSATION","linked_memory_ids":[]}]}'
        )
        == 1
    )


def _real_kira_config(**changes):
    return configured(
        (Suite.CROSS_SESSION,),
        database_url="postgresql://local/eval",
        memory_database_url="postgresql://local/eval",
        gateway_url="https://gateway.test",
        worker_url="https://worker.test",
        kira_base_url="https://kira.test",
        kira_username="shared-operational-user",
        kira_domain="VBI",
        kira_basic_auth=SecretStr("synthetic-basic-secret"),
        kira_context_isolation="unique_username",
    ).model_copy(update=changes)


def _sse_reply(text="synthetic answer"):
    return (
        "data: "
        + json.dumps({"data": {"response": [{"type": "text", "content": {"text": text}}]}})
        + "\n\n"
    )


async def test_real_kira_readiness_authenticates_fresh_identity_and_exhausts_bounded_sse():
    identities = []
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/authenticate":
            identities.append(json.loads(request.content)["username"])
            assert request.headers["authorization"] == "Basic synthetic-basic-secret"
            return httpx.Response(200, json={"errorCode": "00", "content": "synthetic-token"})
        if request.url.path == "/api/v1/chat":
            assert json.loads(request.content)["token"] == "synthetic-token"
            return httpx.Response(200, content=_sse_reply())
        return mock_response(request)

    reports = []
    for _ in range(2):
        reports.append(
            await run_preflight(
                _real_kira_config(),
                transport=httpx.MockTransport(handle),
                database_probe=mock_database,
            )
        )
    assert len(set(identities)) == 2
    assert all(identity.startswith("benchmark_preflight_") for identity in identities)
    check = next(c for c in reports[0].checks if c.probe is Probe.KIRA_CHAT)
    assert check.outcome is Outcome.PASS and check.attempted and not check.simulated
    assert check.kira_event_count == 1 and check.kira_text_bytes == 16
    serialized = reports[0].model_dump_json()
    for forbidden in ["synthetic-token", "synthetic-basic-secret", "synthetic answer", *identities]:
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("auth", Reason.AUTH),
        ("token", Reason.AUTH),
        ("http", Reason.HTTP_STATUS),
        ("empty", Reason.INVALID_CHAT),
        ("whitespace", Reason.INVALID_CHAT),
        ("malformed", Reason.INVALID_CHAT),
        ("oversized_auth", Reason.RESPONSE_TOO_LARGE),
        ("oversized_chat", Reason.RESPONSE_TOO_LARGE),
        ("timeout", Reason.TIMEOUT),
    ],
)
async def test_real_kira_readiness_fails_closed_without_answer_or_credentials(failure, reason):
    def handle(request):
        if request.url.path == "/authenticate":
            if failure == "auth":
                return httpx.Response(403, text="synthetic-basic-secret")
            if failure == "token":
                return httpx.Response(200, json={"errorCode": "99", "content": "secret"})
            if failure == "oversized_auth":
                return httpx.Response(200, content=b"x" * 1025)
            return httpx.Response(200, json={"errorCode": "00", "content": "synthetic-token"})
        if request.url.path == "/api/v1/chat":
            if failure == "http":
                return httpx.Response(500, text="synthetic-basic-secret")
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic-basic-secret")
            content = (
                ""
                if failure == "empty"
                else "data: bad\n"
                if failure == "malformed"
                else _sse_reply("   ")
                if failure == "whitespace"
                else _sse_reply("x" * 1025)
            )
            return httpx.Response(200, content=content)
        return mock_response(request)

    report = await run_preflight(
        _real_kira_config(max_response_bytes=1024),
        transport=httpx.MockTransport(handle),
        database_probe=mock_database,
    )
    check = next(c for c in report.checks if c.probe is Probe.KIRA_CHAT)
    assert check.outcome in {Outcome.DEPENDENCY_ERROR, Outcome.PROTOCOL_ERROR}
    assert check.reason is reason and check.attempted
    assert check.kira_response_sha256 is None
    assert "synthetic-basic-secret" not in report.model_dump_json()


async def test_real_kira_requires_explicit_isolated_identity_approval():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return mock_response(request)

    report = await run_preflight(
        _real_kira_config(kira_context_isolation="disabled"),
        transport=httpx.MockTransport(handle),
        database_probe=mock_database,
    )
    assert not any(path in {"/authenticate", "/api/v1/chat"} for path in calls)
    check = next(c for c in report.checks if c.probe is Probe.KIRA_CHAT)
    assert check.outcome is Outcome.NOT_RUN and not check.attempted


@pytest.mark.parametrize("path", ["/authenticate", "/api/v1/chat"])
async def test_real_kira_bounds_unread_network_stream_and_closes_on_failure(path):
    streams = []

    class ChunkedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * 700
            yield b"x" * 700

        async def aclose(self):
            self.closed = True

    def handle(request):
        if request.url.path == path:
            stream = ChunkedStream()
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        if request.url.path == "/authenticate":
            return httpx.Response(200, json={"errorCode": "00", "content": "synthetic-token"})
        return mock_response(request)

    report = await run_preflight(
        _real_kira_config(max_response_bytes=1024),
        transport=httpx.MockTransport(handle),
        database_probe=mock_database,
    )
    check = next(c for c in report.checks if c.probe is Probe.KIRA_CHAT)
    assert check.outcome is Outcome.PROTOCOL_ERROR and check.reason is Reason.RESPONSE_TOO_LARGE
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "{}",
        '{"verdict":"MAYBE","reason_code":"x"}',
        '{"verdict":"PASS","reason_code":"bad space"}',
        '{"verdict":"PASS","reason_code":"x","raw":"secret"}',
    ],
)
def test_judge_probe_rejects_noncontract_responses(content):
    from evaluation.errors import ProtocolError

    with pytest.raises(ProtocolError):
        judge_observations(content)


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
@pytest.mark.parametrize(
    "reflected_model",
    ["prefix-synthetic-secret-token-suffix", "/models/prefix-synthetic-secret-token-suffix"],
)
async def test_arbitrary_provider_credential_reflection_is_redacted(suite, reflected_model):
    provider = ProviderConfig(
        base_url="https://provider.test/v1",
        model="test-model",
        api_key=SecretStr("synthetic-secret-token"),
    )
    config = EvalConfig(
        profile=Profile.INTERNAL_TEST, suites=(suite,), extraction=provider, embedding=provider
    )
    body = chat_response() if suite == Suite.FORMATION else vector_body()
    body["model"] = reflected_model
    report = await run_preflight(
        config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    assert report.checks[0].returned_model is None
    assert "synthetic-secret-token" not in report.model_dump_json()
