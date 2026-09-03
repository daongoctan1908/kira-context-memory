"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
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

    app_host: str = Field(default="0.0.0.0", min_length=1)
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""
    return Settings()  # type: ignore[call-arg]
