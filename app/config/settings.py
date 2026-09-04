"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, Secret, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    kira_base_url: AnyHttpUrl
    kira_username: str = Field(min_length=1)
    kira_domain: str = Field(default="VBI", min_length=1)
    kira_basic_auth: SecretStr
    kira_service_id: int = Field(default=5, ge=0)
    kira_device: str = Field(default="Browser", min_length=1)
    kira_message_type: str = Field(default="text", min_length=1)
    kira_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    kira_read_timeout_seconds: float = Field(default=300.0, gt=0)
    kira_token_expiry_skew_seconds: float = Field(default=60.0, ge=0)

    database_url: Secret[PostgresDsn] | None = None
    postgres_pool_size: int = Field(default=10, ge=1)
    postgres_max_overflow: int = Field(default=10, ge=0)
    postgres_pool_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_command_timeout_seconds: float = Field(default=5.0, gt=0)

    redis_url: Secret[RedisDsn] | None = None
    redis_max_connections: int = Field(default=20, ge=1)
    redis_connect_timeout_seconds: float = Field(default=1.0, gt=0)
    redis_read_timeout_seconds: float = Field(default=1.0, gt=0)
    redis_health_check_interval_seconds: int = Field(default=30, ge=0)
    redis_session_ttl_seconds: int = Field(default=86400, ge=1)
    max_recent_messages: int = Field(default=10, ge=2, multiple_of=2)

    recent_context_token_budget: int = Field(default=3000, ge=1)
    vllm_base_url: AnyHttpUrl | None = None
    vllm_model: str | None = Field(default=None, min_length=1)
    vllm_api_key: SecretStr | None = None
    vllm_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    vllm_read_timeout_seconds: float = Field(default=8.0, gt=0)
    vllm_max_output_chars: int = Field(default=2048, ge=1)

    app_host: str = Field(default="0.0.0.0", min_length=1)
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""
    return Settings()  # type: ignore[call-arg]
