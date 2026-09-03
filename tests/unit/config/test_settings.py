from pydantic import SecretStr

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
    monkeypatch.setenv("REDIS_URL", "redis://redis.test.internal:6379/2")
    monkeypatch.setenv("REDIS_SESSION_TTL_SECONDS", "7200")
    monkeypatch.setenv("MAX_RECENT_MESSAGES", "8")

    settings = Settings()

    assert str(settings.kira_base_url) == "http://kira.test.internal:8122/"
    assert settings.kira_username == "service-account"
    assert settings.kira_domain == "VBI"
    assert settings.kira_service_id == 9
    assert settings.kira_connect_timeout_seconds == 2.5
    assert settings.database_url is not None
    assert settings.postgres_pool_size == 12
    assert settings.redis_url is not None
    assert str(settings.redis_url.get_secret_value()) == "redis://redis.test.internal:6379/2"
    assert settings.redis_session_ttl_seconds == 7200
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
    assert settings.redis_url is None
    assert settings.redis_max_connections == 20
    assert settings.redis_connect_timeout_seconds == 1.0
    assert settings.redis_read_timeout_seconds == 1.0
    assert settings.redis_health_check_interval_seconds == 30
    assert settings.redis_session_ttl_seconds == 86400
    assert settings.max_recent_messages == 10


def test_settings_hide_redis_credentials_from_repr(monkeypatch) -> None:
    monkeypatch.setenv("KIRA_BASE_URL", "http://127.0.0.1:8122")
    monkeypatch.setenv("KIRA_USERNAME", "service-account")
    monkeypatch.setenv("KIRA_BASIC_AUTH", "secret")
    monkeypatch.setenv("REDIS_URL", "redis://:redis-password@127.0.0.1:6379/0")

    settings = Settings()

    assert "redis-password" not in repr(settings)


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
