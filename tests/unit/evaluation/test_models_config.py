"""Protect the eval data boundary and prevent secrets/ambient runtime config leaks."""

import json
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from evaluation.config import EvalConfig, ProviderConfig, load_config
from evaluation.models import CaseResult, EvalCase, GoldReview, Outcome, Profile, Suite


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
            "split": "dev",
            "provenance": "synthetic",
            "inputs": {"kind": kind, **inputs},
            "gold": {"semantic_expectation": "Giữ attribution của assistant."},
        }
    )
    assert case.suite == kind
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
        },
    )
    assert config.embedding.configured
    assert config.embedding_dimensions == 32
    assert config.read_timeout_seconds == 12
    assert config.extraction_json_mode == "prompt_only"


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
