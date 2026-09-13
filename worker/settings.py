"""Environment-backed settings owned by the asynchronous memory Worker."""

import math
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


class WorkerSettings(BaseSettings):
    """Worker-only configuration with no KiRa or query-rewriter dependency."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    database_url: Secret[PostgresDsn]
    postgres_pool_size: int = Field(default=10, ge=1)
    postgres_max_overflow: int = Field(default=10, ge=0)
    postgres_pool_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_command_timeout_seconds: float = Field(default=5.0, gt=0)
    conversation_operation_timeout_seconds: float = Field(default=5.0, gt=0)

    memory_database_url: Secret[PostgresDsn]
    memory_admin_database_url: Secret[PostgresDsn] | None = None
    memory_schema: str = Field(default="memory", pattern=r"^[a-z_][a-z0-9_]*$")
    memory_collection_name: str = Field(default="memories", pattern=r"^[a-z_][a-z0-9_]*$")
    memory_postgres_min_connections: int = Field(default=1, ge=1)
    memory_postgres_max_connections: int = Field(default=5, ge=1)
    memory_embedding_base_url: AnyHttpUrl
    memory_embedding_model: str = Field(min_length=1)
    memory_embedding_api_key: SecretStr | None = None
    memory_embedding_dims: int = Field(ge=1)
    memory_embedding_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    memory_embedding_read_timeout_seconds: float = Field(default=8.0, gt=0)
    memory_llm_base_url: AnyHttpUrl
    memory_llm_model: str = Field(min_length=1)
    memory_llm_api_key: SecretStr | None = None
    memory_llm_temperature: float = Field(default=0.0, ge=0, le=2)
    memory_llm_max_tokens: int = Field(default=1000, ge=1)
    memory_search_timeout_seconds: float = Field(default=3.0, gt=0)
    memory_operation_timeout_seconds: float = Field(default=30.0, gt=0)
    memory_formation_message_limit: int = Field(default=10, ge=2, multiple_of=2)

    memory_job_poll_interval_seconds: float = Field(default=1.0, gt=0)
    memory_job_batch_size: int = Field(default=10, ge=1)
    memory_job_concurrency: int = Field(default=4, ge=1)
    memory_job_lease_seconds: float = Field(default=120.0, gt=0)
    memory_job_max_attempts: int = Field(default=5, ge=1, le=32_767)
    memory_job_retry_delays_seconds: tuple[float, ...] = (1.0, 5.0, 30.0, 120.0)
    memory_job_db_timeout_seconds: float = Field(default=5.0, gt=0)
    memory_job_shutdown_grace_seconds: float = Field(default=30.0, gt=0)
    memory_job_metrics_refresh_seconds: float = Field(default=15.0, gt=0)
    memory_job_cleanup_interval_seconds: float = Field(default=3600.0, gt=0)
    memory_job_completed_retention_seconds: float = Field(default=604_800.0, gt=0)
    memory_job_dead_retention_seconds: float = Field(default=2_592_000.0, gt=0)
    memory_job_cleanup_batch_size: int = Field(default=1000, ge=1)

    worker_host: str = Field(default="0.0.0.0", min_length=1)
    worker_port: int = Field(default=8001, ge=1, le=65_535)
    worker_log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"

    @model_validator(mode="after")
    def validate_worker_contract(self) -> "WorkerSettings":
        if self.memory_postgres_max_connections < self.memory_postgres_min_connections:
            raise ValueError("memory PostgreSQL max connections must be at least min connections")
        if len(self.memory_job_retry_delays_seconds) != self.memory_job_max_attempts - 1:
            raise ValueError("memory job retry delays must define one delay before each retry")
        if any(
            not math.isfinite(delay) or delay <= 0 for delay in self.memory_job_retry_delays_seconds
        ):
            raise ValueError("memory job retry delays must be finite and positive")
        minimum_lease = (
            self.memory_operation_timeout_seconds + self.conversation_operation_timeout_seconds
        )
        if self.memory_job_lease_seconds <= minimum_lease:
            raise ValueError(
                "memory job lease must exceed the memory operation and conversation read deadlines"
            )
        return self


class MemoryJobAdminSettings(BaseSettings):
    """Minimal PostgreSQL-only settings for the memory-job operator CLI."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    database_url: Secret[PostgresDsn]
    postgres_pool_size: int = Field(default=2, ge=1)
    postgres_max_overflow: int = Field(default=0, ge=0)
    postgres_pool_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    postgres_command_timeout_seconds: float = Field(default=5.0, gt=0)
    memory_job_db_timeout_seconds: float = Field(default=5.0, gt=0)


@lru_cache
def get_worker_settings() -> WorkerSettings:
    """Return the process-wide immutable Worker settings instance."""
    return WorkerSettings()  # type: ignore[call-arg]


@lru_cache
def get_memory_job_admin_settings() -> MemoryJobAdminSettings:
    """Return PostgreSQL-only settings for one operator CLI process."""
    return MemoryJobAdminSettings()  # type: ignore[call-arg]
