from pydantic import SecretStr

from app.config.settings import Settings


def test_settings_read_kira_values_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("KIRA_BASE_URL", "http://kira.test.internal:8122")
    monkeypatch.setenv("KIRA_USERNAME", "service-account")
    monkeypatch.setenv("KIRA_BASIC_AUTH", "top-secret")
    monkeypatch.setenv("KIRA_SERVICE_ID", "9")
    monkeypatch.setenv("KIRA_CONNECT_TIMEOUT_SECONDS", "2.5")

    settings = Settings()

    assert str(settings.kira_base_url) == "http://kira.test.internal:8122/"
    assert settings.kira_username == "service-account"
    assert settings.kira_domain == "VBI"
    assert settings.kira_service_id == 9
    assert settings.kira_connect_timeout_seconds == 2.5
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
