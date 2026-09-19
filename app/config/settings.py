"""Environment-backed application settings."""

from datetime import timedelta
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
    app_version: str = Field(default="0.4.1", min_length=1, max_length=64)
    dev_static_identity_enabled: bool = False
    dev_static_user_id: str | None = Field(default=None, min_length=1)

    auth_enabled: bool = False
    auth_allowed_origin: AnyHttpUrl | None = None
    auth_cookie_secure: bool = False
    auth_session_idle_seconds: int = Field(default=7200, ge=300, le=86400)
    auth_session_absolute_seconds: int = Field(default=28800, ge=900, le=604800)
    auth_session_touch_seconds: int = Field(default=300, ge=60, le=3600)
    auth_lock_threshold: int = Field(default=5, ge=1, le=20)
    auth_lock_seconds: int = Field(default=900, ge=60, le=86400)

    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None
    otel_export_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    otel_batch_schedule_delay_seconds: float = Field(default=5.0, gt=0, le=60)
    otel_batch_max_queue_size: int = Field(default=2048, ge=1, le=65_536)
    otel_batch_max_export_batch_size: int = Field(default=512, ge=1, le=8192)
    otel_metric_export_interval_seconds: float = Field(default=15.0, gt=0, le=300)
    otel_trace_sample_ratio: float = Field(default=1.0, ge=0, le=1)
    otel_shutdown_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    database_url: Secret[PostgresDsn] | None = None
    postgres_pool_size: int = Field(default=10, ge=1)
    postgres_max_overflow: int = Field(default=10, ge=0)
    postgres_pool_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_command_timeout_seconds: float = Field(default=5.0, gt=0)
    conversation_operation_timeout_seconds: float = Field(default=5.0, gt=0)
    chat_request_lease_seconds: float = Field(default=360.0, ge=10, le=900)
    chat_max_body_bytes: int = Field(default=131_072, ge=16_384, le=1_048_576)
    chat_requests_per_minute: int = Field(default=30, ge=1, le=10_000)
    chat_max_concurrent_per_user: int = Field(default=2, ge=1, le=100)

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
        if self.auth_enabled and self.dev_static_identity_enabled:
            raise ValueError("session auth and static development identity are mutually exclusive")
        if self.app_environment == "production" and not self.auth_enabled:
            raise ValueError("application-managed auth is required in production")
        if self.auth_enabled:
            if self.database_url is None:
                raise ValueError("DATABASE_URL is required when auth is enabled")
            if self.auth_allowed_origin is None:
                raise ValueError("AUTH_ALLOWED_ORIGIN is required when auth is enabled")
            if self.auth_session_absolute_seconds < self.auth_session_idle_seconds:
                raise ValueError("auth absolute TTL must be at least the idle TTL")
            if self.auth_session_touch_seconds > self.auth_session_idle_seconds:
                raise ValueError("auth touch interval must not exceed the idle TTL")
        if self.app_environment == "production":
            if not self.auth_cookie_secure:
                raise ValueError("secure auth cookies are required in production")
            assert self.auth_allowed_origin is not None
            if self.auth_allowed_origin.scheme != "https":
                raise ValueError("production auth origin must use HTTPS")
        if self.memory_postgres_max_connections < self.memory_postgres_min_connections:
            raise ValueError("memory PostgreSQL max connections must be at least min connections")
        if self.otel_batch_max_export_batch_size > self.otel_batch_max_queue_size:
            raise ValueError("OTel export batch size must not exceed its queue size")
        if self.otel_exporter_otlp_endpoint is not None and any(
            (
                self.otel_exporter_otlp_endpoint.username,
                self.otel_exporter_otlp_endpoint.password,
                self.otel_exporter_otlp_endpoint.query,
                self.otel_exporter_otlp_endpoint.fragment,
            )
        ):
            raise ValueError("OTel Collector endpoint must not contain credentials or query data")
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
            if self.database_url is not None and self.memory_database_url is not None:
                if _postgres_database_identity(self.database_url) != _postgres_database_identity(
                    self.memory_database_url
                ):
                    raise ValueError(
                        "conversation and memory storage must use the same PostgreSQL database"
                    )
        return self

    @property
    def auth_idle_ttl(self) -> timedelta:
        return timedelta(seconds=self.auth_session_idle_seconds)

    @property
    def auth_absolute_ttl(self) -> timedelta:
        return timedelta(seconds=self.auth_session_absolute_seconds)

    @property
    def auth_touch_interval(self) -> timedelta:
        return timedelta(seconds=self.auth_session_touch_seconds)

    @property
    def auth_lock_duration(self) -> timedelta:
        return timedelta(seconds=self.auth_lock_seconds)

    @property
    def auth_session_cookie_name(self) -> str:
        return "__Host-kira_session" if self.auth_cookie_secure else "kira_session_dev"

    @property
    def auth_csrf_cookie_name(self) -> str:
        return "__Host-kira_csrf" if self.auth_cookie_secure else "kira_csrf_dev"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""
    return Settings()  # type: ignore[call-arg]


def _postgres_database_identity(value: Secret[PostgresDsn]) -> tuple[object, ...]:
    dsn = value.get_secret_value()
    hosts = tuple((str(host["host"]).lower(), host["port"] or 5432) for host in dsn.hosts())
    return hosts, dsn.path
