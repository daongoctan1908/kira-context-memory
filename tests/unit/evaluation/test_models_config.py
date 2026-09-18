"""Protect the eval data boundary and prevent secrets/ambient runtime config leaks."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from evaluation.config import EvalConfig, ProviderConfig, load_config
from evaluation.models import (
    BENCHMARK_CONTRACT_ID,
    HISTORICAL_CONTROL_SHA,
    BenchmarkVariant,
    CandidateChangeScope,
    CandidateDeclaration,
    CaseResult,
    EvalCase,
    GitSource,
    GoldReview,
    Outcome,
    PerformanceReviewVerdict,
    Profile,
    RunProvenance,
    Suite,
)

_HASH = "a" * 64


def provenance(
    variant: BenchmarkVariant,
    *,
    runtime_sha: str = HISTORICAL_CONTROL_SHA,
    candidate: CandidateDeclaration | None = None,
) -> RunProvenance:
    source = GitSource(sha=runtime_sha, dirty=False)
    return RunProvenance(
        variant=variant,
        runtime=source,
        harness=source,
        prompt_sha256={"memory_extraction": _HASH, "rewrite_system": "b" * 64},
        package_versions={
            "kira-context-memory": "0.4.1",
            "viettel-mem0": "2.0.20+viettel.3",
        },
        candidate=candidate,
    )


@pytest.mark.parametrize("kind", list(Suite))
def test_typed_case_round_trip(kind):
    message = {"message_id": "m1", "role": "assistant", "content": "Một đề xuất được xác nhận."}
    inputs = {
        Suite.FORMATION: {"user_id": "synthetic-a", "messages": [message]},
        Suite.RETRIEVAL: {"user_id": "synthetic-a", "current_query": "KPI nào?"},
        Suite.REWRITE: {"current_query": "Tháng trước?", "recent_messages": [message]},
        Suite.CROSS_SESSION: {
            "user_id": "synthetic-a",
            "session_a": "a",
            "session_b": "b",
            "session_a_messages": [message],
            "session_b_query": "Nhắc lại?",
        },
    }[kind]
    case = EvalCase.model_validate(
        {
            "case_id": "case-1",
            "family_id": "confirmation",
            "evaluation_scope": "full_corpus",
            "provenance": "synthetic",
            "source_row_ids": ["source-row-1"],
            "inputs": {"kind": kind, **inputs},
            "gold": {"semantic_expectation": "Giữ attribution của assistant."},
        }
    )
    assert case.suite == kind
    assert case.evaluation_scope == "full_corpus"
    assert EvalCase.model_validate_json(case.model_dump_json()) == case
    assert case.review.status == "draft"
    with pytest.raises(ValidationError):
        case.case_id = "another"


def test_review_and_result_are_not_implicitly_passed():
    with pytest.raises(ValidationError):
        GoldReview(status="reviewed")
    assert GoldReview(status="reviewed", reviewer="mentor", revision="v1").status == "reviewed"
    assert CaseResult(case_id="c1", run_id=uuid4()).outcome == Outcome.NOT_RUN
    with pytest.raises(ValidationError):
        CaseResult(case_id="c1", run_id=uuid4(), outcome="good")


def test_contract_v4_distinguishes_historical_control_and_candidate_scope():
    control = provenance(BenchmarkVariant.HISTORICAL_CONTROL)
    assert BENCHMARK_CONTRACT_ID == "kira-week5-benchmark-v4"
    assert control.attribution_scope == "historical_control"
    assert Outcome.INSUFFICIENT_EVIDENCE == "INSUFFICIENT_EVIDENCE"
    assert set(PerformanceReviewVerdict) == {
        PerformanceReviewVerdict.ACCEPTABLE,
        PerformanceReviewVerdict.REJECT_REGRESSION,
        PerformanceReviewVerdict.NEEDS_MORE_SAMPLES,
    }

    prompt_candidate = CandidateDeclaration(
        candidate_id="prompt-v1",
        change_scopes=(CandidateChangeScope.PROMPT,),
        summary="Change only the rendered extraction prompt.",
    )
    prompt_only = provenance(
        BenchmarkVariant.RELEASE_CANDIDATE,
        runtime_sha="1" * 40,
        candidate=prompt_candidate,
    )
    assert prompt_only.attribution_scope == "prompt_or_config_candidate"

    mixed_candidate = prompt_candidate.model_copy(
        update={
            "candidate_id": "runtime-v1",
            "change_scopes": (
                CandidateChangeScope.PROMPT,
                CandidateChangeScope.RUNTIME_CODE,
            ),
        }
    )
    mixed = provenance(
        BenchmarkVariant.RELEASE_CANDIDATE,
        runtime_sha="2" * 40,
        candidate=mixed_candidate,
    )
    assert mixed.attribution_scope == "mixed_runtime_candidate"
    assert mixed.model_dump(mode="json")["attribution_scope"] == "mixed_runtime_candidate"


def test_candidate_and_historical_control_declarations_fail_closed():
    with pytest.raises(ValidationError):
        provenance(BenchmarkVariant.HISTORICAL_CONTROL, runtime_sha="1" * 40)
    with pytest.raises(ValidationError):
        provenance(BenchmarkVariant.RELEASE_CANDIDATE, runtime_sha="1" * 40)
    with pytest.raises(ValidationError):
        CandidateDeclaration(
            candidate_id="duplicate-scopes",
            change_scopes=(CandidateChangeScope.PROMPT, CandidateChangeScope.PROMPT),
            summary="Invalid duplicate declaration.",
        )
    historical = provenance(BenchmarkVariant.HISTORICAL_CONTROL).model_dump(
        exclude={"attribution_scope"}
    )
    historical["package_versions"]["viettel-mem0"] = "2.0.20+viettel.4"
    with pytest.raises(ValidationError):
        RunProvenance.model_validate(historical)
    historical["package_versions"]["viettel-mem0"] = "2.0.20+viettel.3"
    del historical["prompt_sha256"]["rewrite_system"]
    with pytest.raises(ValidationError):
        RunProvenance.model_validate(historical)


def test_contract_v4_does_not_rewrite_the_historical_control_runtime():
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "docs/week5-baseline.json").read_text(encoding="utf-8"))
    assert manifest["contract_id"] == BENCHMARK_CONTRACT_ID
    assert manifest["variant"] == BenchmarkVariant.HISTORICAL_CONTROL
    assert manifest["source"]["commit"] == HISTORICAL_CONTROL_SHA
    assert manifest["versions"]["viettel_mem0"] == "2.0.20+viettel.3"


def test_cross_session_must_be_distinct_and_case_cannot_claim_real_provenance():
    from evaluation.models import CrossSessionInput

    with pytest.raises(ValidationError):
        CrossSessionInput(
            user_id="a",
            session_a="s",
            session_b="s",
            session_b_query="q",
            session_a_messages=[{"message_id": "m", "role": "user", "content": "q"}],
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.test/v1",
        "https://example.test/v1?key=secret",
        "https://example.test/v1#secret",
        "file:///tmp/provider",
    ],
)
def test_credentials_cannot_hide_in_endpoint_url(url):
    with pytest.raises(ValidationError):
        ProviderConfig(base_url=url)
    with pytest.raises(ValidationError):
        EvalConfig(gateway_url=url)


def test_secret_exclusion_and_no_ambient_env(tmp_path):
    path = tmp_path / ".env.eval"
    path.write_text(
        "OPENAI_API_KEY=sk-synthetic-only\nWEEK5_OPENAI_BASE_URL=https://example.test/v1\n"
        "WEEK5_OPENAI_CHAT_MODEL=file-model\nWEEK5_OPENAI_EMBEDDING_MODEL=embed-model\n"
        "WEEK5_DATABASE_URL=postgresql://u:db-secret@localhost/eval\n",
        encoding="utf-8",
    )
    config = load_config(
        profile=Profile.EXTERNAL_SYNTHETIC,
        suites=(Suite.REWRITE,),
        env_file=path,
        environment={"WEEK5_OPENAI_CHAT_MODEL": "env-model"},
    )
    assert config.rewrite.model == "env-model"
    assert config.embedding.model == "embed-model"
    assert config.extraction.api_key.get_secret_value() == "sk-synthetic-only"
    assert config.rewrite.configured
    for output in (
        repr(config),
        config.model_dump_json(),
        json.dumps(config.model_dump(mode="json")),
    ):
        assert "sk-synthetic-only" not in output
        assert "db-secret" not in output
    other = config.model_copy(update={"database_url": SecretStr("postgresql://u:another@host/db")})
    assert other.fingerprint() == config.fingerprint()
    assert config.model_copy(update={"temperature": 1.0}).fingerprint() != config.fingerprint()


def test_internal_profile_does_not_inherit_openai_and_missing_is_not_config_error():
    config = load_config(
        profile=Profile.INTERNAL_TEST,
        suites=(Suite.FORMATION,),
        environment={
            "OPENAI_API_KEY": "sk-synthetic-only",
            "WEEK5_OPENAI_CHAT_MODEL": "external-model",
            "WEEK5_OPENAI_BASE_URL": "https://api.openai.com/v1",
        },
    )
    assert not config.extraction.configured
    assert config.extraction.api_key is None
    assert not config.embedding.configured
    assert not config.judge.configured
    assert config.judge.api_key is None


def test_judge_never_inherits_shared_external_provider():
    config = load_config(
        profile=Profile.EXTERNAL_SYNTHETIC,
        suites=(Suite.REWRITE,),
        environment={
            "OPENAI_API_KEY": "sk-synthetic-only",
            "WEEK5_OPENAI_CHAT_MODEL": "external-model",
            "WEEK5_OPENAI_BASE_URL": "https://api.openai.com/v1",
        },
    )
    assert not config.judge.configured
    assert config.judge.base_url is None
    assert config.judge.model is None
    assert config.judge.api_key is None


def test_explicit_internal_provider_and_config_knobs():
    config = load_config(
        profile=Profile.INTERNAL_TEST,
        suites=(Suite.RETRIEVAL,),
        environment={
            "WEEK5_EMBEDDING_BASE_URL": "http://internal.test/v1",
            "WEEK5_EMBEDDING_MODEL": "internal-embedding",
            "WEEK5_EMBEDDING_DIMENSIONS": "32",
            "WEEK5_EXTRACTION_JSON_MODE": "prompt_only",
            "WEEK5_READ_TIMEOUT_SECONDS": "12",
            "WEEK5_CONNECT_TIMEOUT_SECONDS": "3",
            "WEEK5_TOTAL_TIMEOUT_SECONDS": "20",
            "WEEK5_JUDGE_BASE_URL": "http://judge.internal/v1",
            "WEEK5_JUDGE_MODEL": "internal-judge",
            "WEEK5_JUDGE_DEPLOYMENT": "judge-test",
            "WEEK5_JUDGE_API_KEY": "internal-secret",
            "WEEK5_JUDGE_MAX_TOKENS": "640",
        },
    )
    assert config.embedding.configured
    assert config.embedding_dimensions == 32
    assert config.read_timeout_seconds == 12
    assert config.extraction_json_mode == "prompt_only"
    assert config.judge.configured
    assert config.judge.model == "internal-judge"
    assert config.judge_deployment == "judge-test"
    assert config.judge_max_tokens == 640
    for output in (repr(config), config.model_dump_json()):
        assert "internal-secret" not in output


def test_custom_endpoint_does_not_receive_shared_openai_key():
    config = load_config(
        profile=Profile.EXTERNAL_SYNTHETIC,
        suites=(Suite.FORMATION,),
        environment={
            "OPENAI_API_KEY": "sk-synthetic-only",
            "WEEK5_OPENAI_BASE_URL": "https://api.openai.com/v1",
            "WEEK5_EXTRACTION_BASE_URL": "https://another.test/v1",
            "WEEK5_EXTRACTION_MODEL": "custom",
        },
    )
    assert config.extraction.api_key is None
    assert not config.extraction.configured


def test_file_interpolation_is_disabled(tmp_path):
    path = tmp_path / ".env.eval"
    path.write_text("OPENAI_API_KEY=${DO_NOT_EXPAND}\n", encoding="utf-8")
    config = load_config(
        profile=Profile.EXTERNAL_SYNTHETIC, suites=(Suite.FORMATION,), env_file=path, environment={}
    )
    assert config.extraction.api_key.get_secret_value() == "${DO_NOT_EXPAND}"
    with pytest.raises(ValueError):
        load_config(
            profile=Profile.INTERNAL_TEST,
            suites=(Suite.REWRITE,),
            env_file=tmp_path / "missing",
            environment={},
        )


@pytest.mark.parametrize(
    "values",
    [
        {"embedding_dimensions": 0},
        {"embedding_dimensions": True},
        {"read_timeout_seconds": float("nan")},
        {"retries": 1},
        {"suites": ()},
        {"database_url": "sqlite:///eval"},
        {"memory_schema": "public;drop"},
        {"extraction": {"model": "sk-synthetic-only"}},
    ],
)
def test_invalid_config_is_rejected(values):
    with pytest.raises(ValidationError):
        EvalConfig(**values)


def test_mock_ignores_secrets_and_missing_files(tmp_path):
    config = load_config(
        profile=Profile.MOCK,
        suites=(Suite.CROSS_SESSION,),
        env_file=tmp_path / "missing",
        environment={"OPENAI_API_KEY": "real-key"},
    )
    assert config.extraction.model == "mock-model"
    assert config.extraction.api_key is None


def test_case_text_preserves_original_whitespace_and_rejects_blank():
    from evaluation.models import RewriteInput

    query = "  Tháng trước?\n"
    assert RewriteInput(current_query=query).current_query == query
    with pytest.raises(ValidationError):
        RewriteInput(current_query=" \n ")
