"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    PostgresDsn,
    Secret,
    SecretStr,
    model_validator,
)
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

    app_environment: Literal["development", "test", "production"] = "development"
    dev_static_identity_enabled: bool = False
    dev_static_user_id: str | None = Field(default=None, min_length=1)

    database_url: Secret[PostgresDsn] | None = None
    postgres_pool_size: int = Field(default=10, ge=1)
    postgres_max_overflow: int = Field(default=10, ge=0)
    postgres_pool_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_command_timeout_seconds: float = Field(default=5.0, gt=0)
    conversation_operation_timeout_seconds: float = Field(default=5.0, gt=0)

    max_recent_messages: int = Field(default=10, ge=2, multiple_of=2)
    recent_context_token_budget: int = Field(default=3000, ge=1)
    vllm_base_url: AnyHttpUrl | None = None
    vllm_model: str | None = Field(default=None, min_length=1)
    vllm_api_key: SecretStr | None = None
    vllm_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    vllm_read_timeout_seconds: float = Field(default=8.0, gt=0)
    vllm_max_output_chars: int = Field(default=2048, ge=1)

    ltm_enabled: bool = False
    memory_formation_enabled: bool = False
    memory_database_url: Secret[PostgresDsn] | None = None
    memory_admin_database_url: Secret[PostgresDsn] | None = None
    memory_schema: str = Field(default="memory", pattern=r"^[a-z_][a-z0-9_]*$")
    memory_collection_name: str = Field(default="memories", pattern=r"^[a-z_][a-z0-9_]*$")
    memory_postgres_min_connections: int = Field(default=1, ge=1)
    memory_postgres_max_connections: int = Field(default=5, ge=1)
    memory_embedding_base_url: AnyHttpUrl | None = None
    memory_embedding_model: str | None = Field(default=None, min_length=1)
    memory_embedding_api_key: SecretStr | None = None
    memory_embedding_dims: int | None = Field(default=None, ge=1)
    memory_embedding_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    memory_embedding_read_timeout_seconds: float = Field(default=8.0, gt=0)
    memory_llm_base_url: AnyHttpUrl | None = None
    memory_llm_model: str | None = Field(default=None, min_length=1)
    memory_llm_api_key: SecretStr | None = None
    memory_llm_temperature: float = Field(default=0.0, ge=0, le=2)
    memory_llm_max_tokens: int = Field(default=1000, ge=1)
    memory_operation_timeout_seconds: float = Field(default=30.0, gt=0)
    memory_formation_message_limit: int = Field(default=10, ge=2, multiple_of=2)
    memory_search_top_k: int = Field(default=10, ge=1, le=10)
    memory_search_threshold: float = Field(default=0.1, ge=0, le=1)
    memory_search_timeout_seconds: float = Field(default=3.0, gt=0)

    app_host: str = Field(default="0.0.0.0", min_length=1)
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"

    @model_validator(mode="after")
    def validate_optional_capabilities(self) -> "Settings":
        if self.dev_static_identity_enabled:
            if self.app_environment == "production":
                raise ValueError("static development identity is forbidden in production")
            if self.dev_static_user_id is None:
                raise ValueError("DEV_STATIC_USER_ID is required when static identity is enabled")
        if self.memory_postgres_max_connections < self.memory_postgres_min_connections:
            raise ValueError("memory PostgreSQL max connections must be at least min connections")
        if self.ltm_enabled:
            required = {
                "MEMORY_DATABASE_URL": self.memory_database_url,
                "MEMORY_EMBEDDING_BASE_URL": self.memory_embedding_base_url,
                "MEMORY_EMBEDDING_MODEL": self.memory_embedding_model,
                "MEMORY_EMBEDDING_DIMS": self.memory_embedding_dims,
                "MEMORY_LLM_BASE_URL": self.memory_llm_base_url,
                "MEMORY_LLM_MODEL": self.memory_llm_model,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(f"LTM configuration is incomplete: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""
    return Settings()  # type: ignore[call-arg]
