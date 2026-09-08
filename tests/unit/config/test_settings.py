import pytest
from pydantic import SecretStr, ValidationError

from app.config.settings import Settings


def test_settings_read_kira_values_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("KIRA_BASE_URL", "http://kira.test.internal:8122")
    monkeypatch.setenv("KIRA_USERNAME", "service-account")
    monkeypatch.setenv("KIRA_BASIC_AUTH", "top-secret")
    monkeypatch.setenv("KIRA_SERVICE_ID", "9")
    monkeypatch.setenv("KIRA_CONNECT_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://gateway:db-secret@postgres.test.internal:5432/kira",
    )
    monkeypatch.setenv("POSTGRES_POOL_SIZE", "12")
    monkeypatch.setenv("MAX_RECENT_MESSAGES", "8")

    settings = Settings()

    assert str(settings.kira_base_url) == "http://kira.test.internal:8122/"
    assert settings.kira_username == "service-account"
    assert settings.kira_domain == "VBI"
    assert settings.kira_service_id == 9
    assert settings.kira_connect_timeout_seconds == 2.5
    assert settings.database_url is not None
    assert settings.postgres_pool_size == 12
    assert settings.max_recent_messages == 8
    assert isinstance(settings.kira_basic_auth, SecretStr)
    assert settings.kira_basic_auth.get_secret_value() == "top-secret"
    assert "top-secret" not in repr(settings)


def test_settings_have_safe_baseline_defaults(monkeypatch) -> None:
    monkeypatch.setenv("KIRA_BASE_URL", "http://127.0.0.1:8122")
    monkeypatch.setenv("KIRA_USERNAME", "service-account")
    monkeypatch.setenv("KIRA_BASIC_AUTH", "secret")

    settings = Settings()

    assert settings.kira_service_id == 5
    assert settings.kira_device == "Browser"
    assert settings.kira_message_type == "text"
    assert settings.kira_connect_timeout_seconds == 5.0
    assert settings.kira_read_timeout_seconds == 300.0
    assert settings.kira_token_expiry_skew_seconds == 60.0
    assert settings.database_url is None
    assert settings.postgres_pool_size == 10
    assert settings.postgres_max_overflow == 10
    assert settings.postgres_pool_timeout_seconds == 2.0
    assert settings.postgres_connect_timeout_seconds == 2.0
    assert settings.postgres_command_timeout_seconds == 5.0
    assert settings.max_recent_messages == 10
    assert settings.recent_context_token_budget == 3000
    assert settings.vllm_base_url is None
    assert settings.vllm_model is None
    assert settings.vllm_api_key is None
    assert settings.vllm_connect_timeout_seconds == 2.0
    assert settings.vllm_read_timeout_seconds == 8.0
    assert settings.vllm_max_output_chars == 2048
    assert settings.dev_static_identity_enabled is False
    assert settings.ltm_enabled is False
    assert settings.memory_schema == "memory"
    assert settings.memory_collection_name == "memories"
    assert settings.memory_formation_message_limit == 10
    assert settings.memory_search_top_k == 10


def test_settings_hide_postgres_credentials_from_repr(monkeypatch) -> None:
    monkeypatch.setenv("KIRA_BASE_URL", "http://127.0.0.1:8122")
    monkeypatch.setenv("KIRA_USERNAME", "service-account")
    monkeypatch.setenv("KIRA_BASIC_AUTH", "secret")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://gateway:database-password@127.0.0.1:5432/kira",
    )

    settings = Settings()

    assert "database-password" not in repr(settings)


def test_rewriter_settings_read_environment_and_hide_api_key(monkeypatch) -> None:
    monkeypatch.setenv("KIRA_BASE_URL", "http://kira.test")
    monkeypatch.setenv("KIRA_USERNAME", "service-account")
    monkeypatch.setenv("KIRA_BASIC_AUTH", "secret")
    monkeypatch.setenv("RECENT_CONTEXT_TOKEN_BUDGET", "1000")
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.test:8000/v1")
    monkeypatch.setenv("VLLM_MODEL", "environment-model")
    monkeypatch.setenv("VLLM_API_KEY", "private-vllm-key")
    monkeypatch.setenv("VLLM_CONNECT_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("VLLM_READ_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("VLLM_MAX_OUTPUT_CHARS", "1024")

    settings = Settings(_env_file=None)

    assert settings.recent_context_token_budget == 1000
    assert str(settings.vllm_base_url) == "http://vllm.test:8000/v1"
    assert settings.vllm_model == "environment-model"
    assert settings.vllm_connect_timeout_seconds == 1.5
    assert settings.vllm_read_timeout_seconds == 4
    assert settings.vllm_max_output_chars == 1024
    assert settings.vllm_api_key.get_secret_value() == "private-vllm-key"
    assert "private-vllm-key" not in repr(settings)


@pytest.mark.parametrize(
    "field",
    [
        "recent_context_token_budget",
        "vllm_connect_timeout_seconds",
        "vllm_read_timeout_seconds",
        "vllm_max_output_chars",
    ],
)
def test_rewriter_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            kira_base_url="http://kira.test",
            kira_username="service-account",
            kira_basic_auth="secret",
            **{field: 0},
        )


def test_static_identity_is_forbidden_in_production() -> None:
    with pytest.raises(ValidationError, match="static development identity"):
        Settings(
            _env_file=None,
            kira_base_url="http://kira.test",
            kira_username="service-account",
            kira_basic_auth="secret",
            app_environment="production",
            dev_static_identity_enabled=True,
            dev_static_user_id="unsafe-user",
        )


def test_enabled_ltm_requires_all_runtime_dependencies() -> None:
    with pytest.raises(ValidationError, match="MEMORY_DATABASE_URL"):
        Settings(
            _env_file=None,
            kira_base_url="http://kira.test",
            kira_username="service-account",
            kira_basic_auth="secret",
            ltm_enabled=True,
        )


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_memory_formation_limit_must_preserve_turn_pairs(limit: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            kira_base_url="http://kira.test",
            kira_username="service-account",
            kira_basic_auth="secret",
            memory_formation_message_limit=limit,
        )


@pytest.mark.parametrize("limit", [0, 11])
def test_memory_search_top_k_is_bounded_to_c1_context_limit(limit: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            kira_base_url="http://kira.test",
            kira_username="service-account",
            kira_basic_auth="secret",
            memory_search_top_k=limit,
        )


def test_memory_secrets_are_redacted() -> None:
    settings = Settings(
        _env_file=None,
        kira_base_url="http://kira.test",
        kira_username="service-account",
        kira_basic_auth="secret",
        memory_database_url="postgresql://user:memory-password@db/memory",
        memory_embedding_api_key="embedding-secret",
        memory_llm_api_key="llm-secret",
    )

    rendered = repr(settings)
    assert "memory-password" not in rendered
    assert "embedding-secret" not in rendered
    assert "llm-secret" not in rendered
