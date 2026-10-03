"""Internal-only semantic judge transport and fail-closed protocol tests."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from evaluation.config import EvalConfig, ProviderConfig
from evaluation.judge import InternalSemanticJudge, JudgeError
from evaluation.models import Outcome, Profile, Suite
from evaluation.scoring import FormationMatchVerdict, JudgeVerdict, output_sha256


def _config(**overrides) -> EvalConfig:
    values = {
        "profile": Profile.INTERNAL_TEST,
        "suites": (Suite.FORMATION, Suite.REWRITE, Suite.CROSS_SESSION),
        "judge": ProviderConfig(
            base_url="https://judge.internal/v1",
            model="judge-model",
            api_key=SecretStr("internal-secret"),
        ),
        "judge_deployment": "judge-test",
        "judge_max_tokens": 321,
    }
    values.update(overrides)
    return EvalConfig(**values)


def _chat(content: object, *, finish_reason: str = "stop") -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": text},
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_semantic_judge_is_blind_strict_and_binds_output_hash():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _chat(
            {
                "verdict": "PASS",
                "reason_code": "preserves_intent",
                "rationale": "Intent and constraints are preserved.",
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        judge = InternalSemanticJudge(client, _config())
        output = {"answer": "So sánh KPI tại Hà Nội"}
        result = await judge.semantic(
            case_id="rewrite-1",
            suite=Suite.REWRITE,
            output=output,
            semantic_expectation="Keep the KPI and location.",
            reference_answer="A valid answer must compare the KPI in Hà Nội.",
            required_exact=("KPI", "Hà Nội"),
            forbidden=("TP.HCM",),
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://judge.internal/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer internal-secret"
    body = json.loads(request.content)
    assert body["model"] == "judge-model"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 321
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    serialized = json.dumps(body)
    assert "variant" not in serialized
    assert "control" not in serialized
    assert "candidate_id" not in serialized
    user_payload = json.loads(body["messages"][1]["content"])
    assert user_payload["reference_answer"].startswith("A valid answer")
    assert result.verdict is JudgeVerdict.PASS
    assert result.output_sha256 == output_sha256(output)
    assert result.judge.profile is Profile.INTERNAL_TEST
    assert result.judge.provider == "internal_openai_compatible"
    assert result.judge.deployment == "judge-test"
    assert len(result.judge.prompt_sha256) == 64
    assert len(result.judge.response_schema_sha256) == 64


@pytest.mark.asyncio
async def test_formation_judge_requires_exact_one_to_one_decision_set():
    def handler(_: httpx.Request) -> httpx.Response:
        return _chat(
            {
                "decisions": [
                    {
                        "prediction_index": 2,
                        "verdict": "NO_MATCH",
                        "gold_id": None,
                        "reason_code": "unsupported_fact",
                    },
                    {
                        "prediction_index": 0,
                        "verdict": "MATCH",
                        "gold_id": "g1",
                        "reason_code": "semantic_equivalent",
                    },
                ]
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        decisions = await InternalSemanticJudge(client, _config()).formation(
            predicted_facts=("Phản hồi súc tích", "exact fact", "Không có bằng chứng"),
            gold_facts={"g1": "Trả lời ngắn gọn"},
            prediction_indexes=(0, 2),
        )

    assert [decision.prediction_index for decision in decisions] == [0, 2]
    assert decisions[0].verdict is FormationMatchVerdict.MATCH
    assert decisions[0].gold_id == "g1"
    assert decisions[1].verdict is FormationMatchVerdict.NO_MATCH


@pytest.mark.asyncio
async def test_open_world_judge_receives_boundary_attribution_scope_and_forbidden_target():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _chat(
            {
                "decisions": [
                    {
                        "prediction_index": 0,
                        "verdict": "VALID_EXTRA",
                        "gold_id": None,
                        "reason_code": "source_reassertion",
                    }
                ]
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        decisions = await InternalSemanticJudge(client, _config()).formation(
            predicted_facts=("Ngưỡng 98%, chỉ cho FTTH",),
            gold_facts={},
            prediction_indexes=(0,),
            contract="open_world",
            source_messages=({"role": "user", "content": "Quay lại 98%, chỉ FTTH"},),
            context_messages=({"role": "user", "content": "Ngưỡng 99%"},),
            prediction_details=({"attributed_to": "user", "memory_scope": "GLOBAL"},),
            semantic_expectation="Keep reusable conventions and change statements.",
            forbidden_facts=("Mật khẩu",),
        )
    payload = json.loads(requests[0]["messages"][1]["content"])
    assert payload["formation_contract"] == "open_world"
    assert payload["source_messages"][0]["content"] == "Quay lại 98%, chỉ FTTH"
    assert payload["context_messages"][0]["content"] == "Ngưỡng 99%"
    assert payload["predictions"][0]["memory_scope"] == "GLOBAL"
    assert payload["predictions"][0]["attributed_to"] == "user"
    assert payload["forbidden_facts"] == ["Mật khẩu"]
    assert "final current truth" in requests[0]["messages"][0]["content"]
    assert decisions[0].verdict is FormationMatchVerdict.VALID_EXTRA


@pytest.mark.asyncio
async def test_judge_rejects_valid_extra_for_closed_world_and_missing_open_world_source():
    response = {
        "decisions": [
            {
                "prediction_index": 0,
                "verdict": "VALID_EXTRA",
                "gold_id": None,
                "reason_code": "unexpected_extra",
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _chat(response))
    ) as client:
        judge = InternalSemanticJudge(client, _config())
        with pytest.raises(JudgeError) as captured:
            await judge.formation(predicted_facts=("A",), gold_facts={}, prediction_indexes=(0,))
        assert captured.value.reason_code == "judge_invalid_extra_contract"
        with pytest.raises(ValueError, match="source messages"):
            await judge.formation(
                predicted_facts=("A",),
                gold_facts={},
                prediction_indexes=(0,),
                contract="open_world",
            )
        with pytest.raises(ValueError, match="metadata must align"):
            await judge.formation(
                predicted_facts=("A",),
                gold_facts={},
                prediction_indexes=(0,),
                prediction_details=({}, {}),
            )


@pytest.mark.parametrize("profile", [Profile.MOCK, Profile.EXTERNAL_SYNTHETIC])
def test_judge_refuses_non_internal_profiles(profile):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _chat({})))
    try:
        with pytest.raises(ValueError, match="approved acceptance"):
            InternalSemanticJudge(client, _config(profile=profile))
    finally:
        asyncio.run(client.aclose())


def test_judge_requires_explicit_provider():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _chat({})))
    try:
        with pytest.raises(ValueError, match="explicit endpoint"):
            InternalSemanticJudge(client, _config(judge=ProviderConfig()))
    finally:
        asyncio.run(client.aclose())


def test_pc_acceptance_judge_records_external_provider_provenance():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _chat({})))
    try:
        judge = InternalSemanticJudge(
            client,
            _config(profile=Profile.PC_OPENAI_ACCEPTANCE),
        )
        provenance = judge._provenance(prompt="prompt", response_model=type(_config()))
        assert provenance.profile is Profile.PC_OPENAI_ACCEPTANCE
        assert provenance.provider == "openai_external"
    finally:
        asyncio.run(client.aclose())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"not_choices": []}, "judge_invalid_chat"),
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ]
            },
            "judge_invalid_chat",
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "not-json"},
                    }
                ]
            },
            "judge_invalid_json",
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"verdict":"PASS"}'},
                    }
                ]
            },
            "judge_invalid_schema",
        ),
    ],
)
async def test_semantic_judge_rejects_invalid_protocol_without_raw_output(payload, reason):
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(JudgeError) as captured:
            await InternalSemanticJudge(client, _config()).semantic(
                case_id="rewrite-1",
                suite=Suite.REWRITE,
                output="candidate output",
                semantic_expectation="expected meaning",
            )
    assert calls == 1
    assert captured.value.outcome is Outcome.PROTOCOL_ERROR
    assert captured.value.reason_code == reason
    assert str(captured.value) == reason
    assert "candidate output" not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "judge_authentication"), (429, "judge_rate_limit"), (503, "judge_http_status")],
)
async def test_judge_dependency_failure_is_safe_and_has_no_retry(status, reason):
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="internal-secret provider details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(JudgeError) as captured:
            await InternalSemanticJudge(client, _config()).semantic(
                case_id="rewrite-1",
                suite=Suite.REWRITE,
                output="output",
                semantic_expectation="expectation",
            )
    assert calls == 1
    assert captured.value.outcome is Outcome.DEPENDENCY_ERROR
    assert captured.value.reason_code == reason
    assert captured.value.http_status == status
    assert "internal-secret" not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decisions", "reason"),
    [
        (
            [
                {
                    "prediction_index": 0,
                    "verdict": "NO_MATCH",
                    "gold_id": None,
                    "reason_code": "different",
                }
            ],
            "judge_invalid_prediction_set",
        ),
        (
            [
                {
                    "prediction_index": index,
                    "verdict": "MATCH",
                    "gold_id": "g1",
                    "reason_code": "equivalent",
                }
                for index in (0, 1)
            ],
            "judge_invalid_gold_mapping",
        ),
    ],
)
async def test_formation_judge_rejects_missing_or_duplicate_mappings(decisions, reason):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _chat({"decisions": decisions}))
    ) as client:
        with pytest.raises(JudgeError) as captured:
            await InternalSemanticJudge(client, _config()).formation(
                predicted_facts=("a", "b"),
                gold_facts={"g1": "gold"},
                prediction_indexes=(0, 1),
            )
    assert captured.value.reason_code == reason


@pytest.mark.asyncio
async def test_judge_response_size_is_bounded():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 2048))
    ) as client:
        config = _config(max_response_bytes=1024)
        with pytest.raises(JudgeError) as captured:
            await InternalSemanticJudge(client, config).semantic(
                case_id="rewrite-1",
                suite=Suite.REWRITE,
                output="output",
                semantic_expectation="expectation",
            )
    assert captured.value.outcome is Outcome.PROTOCOL_ERROR
    assert captured.value.reason_code == "judge_response_too_large"


@pytest.mark.asyncio
async def test_judge_enforces_total_timeout():
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return _chat({})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = _config(total_timeout_seconds=0.001)
        with pytest.raises(JudgeError) as captured:
            await InternalSemanticJudge(client, config).semantic(
                case_id="rewrite-1",
                suite=Suite.REWRITE,
                output="output",
                semantic_expectation="expectation",
            )
    assert captured.value.outcome is Outcome.DEPENDENCY_ERROR
    assert captured.value.reason_code == "judge_timeout"
