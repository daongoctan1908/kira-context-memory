"""Narrow structural settings contracts shared by process composition roots."""

from typing import Protocol

from pydantic import AnyHttpUrl, PostgresDsn, Secret, SecretStr


class PostgresRuntimeSettings(Protocol):
    """Settings required to construct the shared async PostgreSQL engine."""

    database_url: Secret[PostgresDsn] | None
    postgres_pool_size: int
    postgres_max_overflow: int
    postgres_pool_timeout_seconds: float
    postgres_connect_timeout_seconds: float
    postgres_command_timeout_seconds: float


class MemoryRuntimeSettings(Protocol):
    """Settings required by the Mem0 runtime adapter."""

    memory_database_url: Secret[PostgresDsn] | None
    memory_schema: str
    memory_collection_name: str
    memory_postgres_min_connections: int
    memory_postgres_max_connections: int
    memory_embedding_base_url: AnyHttpUrl | None
    memory_embedding_model: str | None
    memory_embedding_api_key: SecretStr | None
    memory_embedding_dims: int | None
    memory_llm_base_url: AnyHttpUrl | None
    memory_llm_model: str | None
    memory_llm_api_key: SecretStr | None
    memory_llm_temperature: float
    memory_llm_max_tokens: int
    memory_search_timeout_seconds: float
    memory_operation_timeout_seconds: float


class MemoryAdminRuntimeSettings(Protocol):
    """Settings required to initialize or validate the pgvector memory schema."""

    memory_database_url: Secret[PostgresDsn] | None
    memory_admin_database_url: Secret[PostgresDsn] | None
    memory_schema: str
    memory_collection_name: str
    memory_embedding_base_url: AnyHttpUrl | None
    memory_embedding_model: str | None
    memory_embedding_api_key: SecretStr | None
    memory_embedding_dims: int | None
    memory_embedding_connect_timeout_seconds: float
    memory_embedding_read_timeout_seconds: float
