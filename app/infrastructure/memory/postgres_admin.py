"""Explicit pgvector schema initialization; runtime adapters never issue DDL."""

import asyncio
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

import httpx
import psycopg
from psycopg import sql

from app.config.runtime_contracts import MemoryAdminRuntimeSettings
from app.domain.errors.memory import (
    LongTermMemoryConfigurationError,
    LongTermMemoryConnectionError,
    LongTermMemoryOperationError,
    LongTermMemoryProtocolError,
    LongTermMemoryTimeoutError,
)

MEMORY_SCHEMA_VERSION = 1
MEM0_DISTRIBUTION = "viettel-mem0"


@dataclass(frozen=True, slots=True)
class MemorySchemaState:
    schema_version: int
    embedding_model: str
    embedding_dims: int
    mem0_version: str
    pgvector_version: str


def normalize_psycopg_dsn(value: str) -> str:
    """Convert the SQLAlchemy async PostgreSQL scheme to psycopg conninfo."""
    if value.startswith("postgresql+asyncpg://"):
        return value.replace("postgresql+asyncpg://", "postgresql://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql://", 1)
    if value.startswith("postgresql://"):
        return value
    raise LongTermMemoryConfigurationError


def embeddings_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/embeddings"
    return f"{normalized}/v1/embeddings"


async def probe_embedding_dimension(
    settings: MemoryAdminRuntimeSettings,
    client: httpx.AsyncClient,
) -> int:
    """Verify that the configured OpenAI-compatible embedder returns the pinned dimension."""
    if (
        settings.memory_embedding_base_url is None
        or settings.memory_embedding_model is None
        or settings.memory_embedding_dims is None
    ):
        raise LongTermMemoryConfigurationError

    headers: dict[str, str] = {}
    if settings.memory_embedding_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.memory_embedding_api_key.get_secret_value()}"
    try:
        response = await client.post(
            embeddings_url(str(settings.memory_embedding_base_url)),
            headers=headers,
            json={
                "model": settings.memory_embedding_model,
                "input": "kira-memory-dimension-probe",
                "encoding_format": "float",
            },
            timeout=httpx.Timeout(
                connect=settings.memory_embedding_connect_timeout_seconds,
                read=settings.memory_embedding_read_timeout_seconds,
                write=settings.memory_embedding_connect_timeout_seconds,
                pool=settings.memory_embedding_connect_timeout_seconds,
            ),
        )
        response.raise_for_status()
    except httpx.TimeoutException as error:
        raise LongTermMemoryTimeoutError from error
    except httpx.RequestError as error:
        raise LongTermMemoryConnectionError from error
    except httpx.HTTPStatusError as error:
        raise LongTermMemoryOperationError from error

    try:
        embedding = response.json()["data"][0]["embedding"]
        if (
            not isinstance(embedding, list)
            or not embedding
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float)) for item in embedding
            )
        ):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise LongTermMemoryProtocolError from error

    actual = len(embedding)
    if actual != settings.memory_embedding_dims:
        raise LongTermMemoryConfigurationError
    return actual


async def initialize_memory_schema(
    settings: MemoryAdminRuntimeSettings,
    client: httpx.AsyncClient | None = None,
) -> MemorySchemaState:
    """Probe embeddings, then create or validate the versioned memory schema."""
    database_secret = settings.memory_admin_database_url or settings.memory_database_url
    if (
        database_secret is None
        or settings.memory_embedding_model is None
        or settings.memory_embedding_dims is None
    ):
        raise LongTermMemoryConfigurationError

    owned_client = client is None
    resolved_client = client or httpx.AsyncClient()
    try:
        await probe_embedding_dimension(settings, resolved_client)
    finally:
        if owned_client:
            await resolved_client.aclose()

    dsn = normalize_psycopg_dsn(str(database_secret.get_secret_value()))
    try:
        return await asyncio.to_thread(
            _initialize_memory_schema_sync,
            dsn,
            settings.memory_schema,
            settings.memory_collection_name,
            settings.memory_embedding_model,
            settings.memory_embedding_dims,
        )
    except LongTermMemoryConfigurationError:
        raise
    except (psycopg.OperationalError, TimeoutError, OSError) as error:
        raise LongTermMemoryConnectionError from error
    except psycopg.Error as error:
        raise LongTermMemoryOperationError from error


async def validate_memory_schema(
    settings: MemoryAdminRuntimeSettings,
) -> MemorySchemaState:
    """Read and validate the existing memory schema without issuing DDL."""
    if (
        settings.memory_database_url is None
        or settings.memory_embedding_model is None
        or settings.memory_embedding_dims is None
    ):
        raise LongTermMemoryConfigurationError

    dsn = normalize_psycopg_dsn(str(settings.memory_database_url.get_secret_value()))
    try:
        return await asyncio.to_thread(
            _validate_memory_schema_sync,
            dsn,
            settings.memory_schema,
            settings.memory_collection_name,
            settings.memory_embedding_model,
            settings.memory_embedding_dims,
        )
    except LongTermMemoryConfigurationError:
        raise
    except (psycopg.OperationalError, TimeoutError, OSError) as error:
        raise LongTermMemoryConnectionError from error
    except psycopg.Error as error:
        raise LongTermMemoryOperationError from error


def _validate_memory_schema_sync(
    dsn: str,
    schema_name: str,
    collection_name: str,
    embedding_model: str,
    embedding_dims: int,
) -> MemorySchemaState:
    """Validate version metadata and vector dimensions using read-only statements."""
    try:
        mem0_version = version(MEM0_DISTRIBUTION)
    except PackageNotFoundError as error:
        raise LongTermMemoryConfigurationError from error

    metadata_table_name = f"{schema_name}.kira_memory_schema"
    metadata_table = sql.Identifier(schema_name, "kira_memory_schema")
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        extension_row = cursor.fetchone()
        if not extension_row or not isinstance(extension_row[0], str):
            raise LongTermMemoryConfigurationError
        pgvector_version = extension_row[0]

        cursor.execute("SELECT to_regclass(%s)", (metadata_table_name,))
        metadata_registration = cursor.fetchone()
        if not metadata_registration or metadata_registration[0] is None:
            raise LongTermMemoryConfigurationError
        cursor.execute(
            sql.SQL(
                "SELECT schema_version, embedding_model, embedding_dims, "
                "mem0_version, pgvector_version FROM {} WHERE singleton"
            ).format(metadata_table)
        )
        row = cursor.fetchone()
        expected = (
            MEMORY_SCHEMA_VERSION,
            embedding_model,
            embedding_dims,
            mem0_version,
            pgvector_version,
        )
        if row is None or tuple(row) != expected:
            raise LongTermMemoryConfigurationError

        for table_name in (collection_name, f"{collection_name}_entities"):
            cursor.execute(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute AS a "
                "WHERE a.attrelid = to_regclass(%s) AND a.attname = 'vector'",
                (f"{schema_name}.{table_name}",),
            )
            if cursor.fetchone() != (f"vector({embedding_dims})",):
                raise LongTermMemoryConfigurationError

    return MemorySchemaState(*expected)


def _initialize_memory_schema_sync(
    dsn: str,
    schema_name: str,
    collection_name: str,
    embedding_model: str,
    embedding_dims: int,
) -> MemorySchemaState:
    try:
        mem0_version = version(MEM0_DISTRIBUTION)
    except PackageNotFoundError as error:
        raise LongTermMemoryConfigurationError from error

    metadata_table = sql.Identifier(schema_name, "kira_memory_schema")
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('kira-memory-schema-v1'))")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cursor.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema_name))
        )
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        extension_row = cursor.fetchone()
        if not extension_row or not isinstance(extension_row[0], str):
            raise LongTermMemoryConfigurationError
        pgvector_version = extension_row[0]

        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                    schema_version SMALLINT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding_dims INTEGER NOT NULL CHECK (embedding_dims > 0),
                    mem0_version TEXT NOT NULL,
                    pgvector_version TEXT NOT NULL,
                    initialized_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ).format(metadata_table)
        )
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {} (
                    singleton, schema_version, embedding_model, embedding_dims,
                    mem0_version, pgvector_version
                ) VALUES (TRUE, %s, %s, %s, %s, %s)
                ON CONFLICT (singleton) DO NOTHING
                """
            ).format(metadata_table),
            (
                MEMORY_SCHEMA_VERSION,
                embedding_model,
                embedding_dims,
                mem0_version,
                pgvector_version,
            ),
        )
        cursor.execute(
            sql.SQL(
                "SELECT schema_version, embedding_model, embedding_dims, "
                "mem0_version, pgvector_version FROM {} WHERE singleton"
            ).format(metadata_table)
        )
        row = cursor.fetchone()
        expected = (
            MEMORY_SCHEMA_VERSION,
            embedding_model,
            embedding_dims,
            mem0_version,
            pgvector_version,
        )
        if row is None or tuple(row) != expected:
            raise LongTermMemoryConfigurationError

        for table_name in (collection_name, f"{collection_name}_entities"):
            collection_table = sql.Identifier(schema_name, table_name)
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} "
                    "(id UUID PRIMARY KEY, vector vector({}), payload JSONB NOT NULL)"
                ).format(collection_table, sql.Literal(embedding_dims))
            )
            cursor.execute(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute AS a "
                "WHERE a.attrelid = to_regclass(%s) AND a.attname = 'vector'",
                (f"{schema_name}.{table_name}",),
            )
            vector_type = cursor.fetchone()
            if vector_type != (f"vector({embedding_dims})",):
                raise LongTermMemoryConfigurationError
            cursor.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw (vector vector_cosine_ops)"
                ).format(
                    sql.Identifier(f"{table_name}_hnsw_idx"),
                    collection_table,
                )
            )
            cursor.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ((payload->>'user_id'))").format(
                    sql.Identifier(f"{table_name}_user_id_idx"),
                    collection_table,
                )
            )
            cursor.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} "
                    "USING gin(to_tsvector('simple', payload->>'text_lemmatized'))"
                ).format(
                    sql.Identifier(f"{table_name}_text_lemmatized_idx"),
                    collection_table,
                )
            )

    return MemorySchemaState(*expected)
