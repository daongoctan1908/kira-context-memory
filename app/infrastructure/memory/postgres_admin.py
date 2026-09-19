"""Explicit pgvector schema initialization; runtime adapters never issue DDL."""

import asyncio
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from uuid import UUID

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

MEMORY_SCHEMA_VERSION = 3
MEM0_DISTRIBUTION = "viettel-mem0"
LEGACY_MEMORY_SCHEMA_VERSION = 1
LEGACY_MEM0_VERSION = "2.0.20+viettel.2"
PREVIOUS_MEMORY_SCHEMA_VERSION = 2
PREVIOUS_MEM0_SCHEMA_CONTRACT_VERSION = "2.0.20+viettel.3"
COMPATIBLE_MEMORY_SCHEMA_VERSION = 3
COMPATIBLE_MEM0_SCHEMA_CONTRACT_VERSION = "2.0.20+viettel.5"
MEM0_SCHEMA_CONTRACT_VERSION = "2.0.20+viettel.6"
CURRENT_MEM0_DISTRIBUTION_VERSION = "2.0.20+viettel.6"
FORMATION_RECEIPT_SUFFIX = "_formation_receipts"


@dataclass(frozen=True, slots=True)
class MemorySchemaState:
    schema_version: int
    embedding_model: str
    embedding_dims: int
    mem0_version: str
    pgvector_version: str


def memory_schema_contract_version() -> str:
    """Return the persisted contract version for the installed compatible Mem0 build."""
    try:
        installed_version = version(MEM0_DISTRIBUTION)
    except PackageNotFoundError as error:
        raise LongTermMemoryConfigurationError from error
    if installed_version != CURRENT_MEM0_DISTRIBUTION_VERSION:
        raise LongTermMemoryConfigurationError
    return MEM0_SCHEMA_CONTRACT_VERSION


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


def formation_receipt_table_name(collection_name: str) -> str:
    """Return the collection-scoped durable formation receipt table name."""
    return f"{collection_name}{FORMATION_RECEIPT_SUFFIX}"


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
    mem0_version = memory_schema_contract_version()

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

        _validate_formation_schema(cursor, schema_name, collection_name)

    return MemorySchemaState(*expected)


def _initialize_memory_schema_sync(
    dsn: str,
    schema_name: str,
    collection_name: str,
    embedding_model: str,
    embedding_dims: int,
) -> MemorySchemaState:
    mem0_version = memory_schema_contract_version()

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
        legacy = (
            LEGACY_MEMORY_SCHEMA_VERSION,
            embedding_model,
            embedding_dims,
            LEGACY_MEM0_VERSION,
            pgvector_version,
        )
        previous = (
            PREVIOUS_MEMORY_SCHEMA_VERSION,
            embedding_model,
            embedding_dims,
            PREVIOUS_MEM0_SCHEMA_CONTRACT_VERSION,
            pgvector_version,
        )
        compatible = (
            COMPATIBLE_MEMORY_SCHEMA_VERSION,
            embedding_model,
            embedding_dims,
            COMPATIBLE_MEM0_SCHEMA_CONTRACT_VERSION,
            pgvector_version,
        )
        if row is None or tuple(row) not in (expected, legacy, previous, compatible):
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
            if table_name == collection_name:
                cursor.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {} "
                        "((payload->>'formation_event_id')) "
                        "WHERE payload ? 'formation_event_id'"
                    ).format(
                        sql.Identifier(f"{collection_name}_formation_event_id_idx"),
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

        receipt_table = sql.Identifier(
            schema_name,
            formation_receipt_table_name(collection_name),
        )
        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    event_id UUID PRIMARY KEY,
                    user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
                    conversation_id UUID NOT NULL,
                    result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'array'),
                    memory_count INTEGER NOT NULL CHECK (
                        memory_count >= 0 AND memory_count = jsonb_array_length(result)
                    ),
                    committed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ).format(receipt_table)
        )

        receipt_owner_index = sql.Identifier(f"{collection_name}_formation_receipt_owner_idx")
        if tuple(row) == previous:
            _backfill_receipt_conversation_ownership(
                cursor,
                schema_name,
                collection_name,
            )
        cursor.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (user_id, conversation_id)").format(
                receipt_owner_index,
                receipt_table,
            )
        )

        if tuple(row) in (legacy, previous, compatible):
            cursor.execute(
                sql.SQL(
                    "UPDATE {} SET schema_version = %s, mem0_version = %s "
                    "WHERE singleton AND schema_version = %s AND mem0_version = %s"
                ).format(metadata_table),
                (
                    MEMORY_SCHEMA_VERSION,
                    mem0_version,
                    row[0],
                    row[3],
                ),
            )
            if cursor.rowcount != 1:
                raise LongTermMemoryConfigurationError
        _validate_formation_schema(cursor, schema_name, collection_name)

    return MemorySchemaState(*expected)


def _backfill_receipt_conversation_ownership(
    cursor,
    schema_name: str,
    collection_name: str,
) -> None:
    """Upgrade v2 receipts without guessing ownership for unresolvable events."""

    receipt_table = sql.Identifier(
        schema_name,
        formation_receipt_table_name(collection_name),
    )
    memory_table = sql.Identifier(schema_name, collection_name)
    cursor.execute(
        sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS conversation_id UUID").format(
            receipt_table
        )
    )
    cursor.execute(
        sql.SQL(
            "SELECT r.event_id::text, r.user_id, "
            "m.payload->>'conversation_id', m.payload->>'user_id' "
            "FROM {} AS r JOIN {} AS m "
            "ON m.payload->>'formation_event_id' = r.event_id::text "
            "WHERE r.conversation_id IS NULL ORDER BY r.event_id"
        ).format(receipt_table, memory_table)
    )
    payload_owners: dict[tuple[str, str], set[str]] = {}
    invalid_events: set[str] = set()
    for event_id, receipt_user_id, conversation_id, payload_user_id in cursor.fetchall():
        if payload_user_id != receipt_user_id:
            invalid_events.add(event_id)
            continue
        try:
            normalized = str(UUID(conversation_id))
        except (ValueError, AttributeError, TypeError):
            invalid_events.add(event_id)
            continue
        payload_owners.setdefault((event_id, receipt_user_id), set()).add(normalized)

    for (event_id, user_id), owners in payload_owners.items():
        if len(owners) != 1 or event_id in invalid_events:
            continue
        cursor.execute(
            sql.SQL(
                "UPDATE {} SET conversation_id = %s "
                "WHERE event_id = %s AND user_id = %s AND conversation_id IS NULL"
            ).format(receipt_table),
            (next(iter(owners)), event_id, user_id),
        )

    cursor.execute(
        "SELECT to_regclass('public.memory_jobs'), "
        "to_regclass('public.conversation_messages'), "
        "to_regclass('public.conversations')"
    )
    app_tables = cursor.fetchone()
    if app_tables and all(table is not None for table in app_tables):
        cursor.execute(
            sql.SQL(
                "UPDATE {} AS r SET conversation_id = c.conversation_id "
                "FROM public.memory_jobs AS j "
                "JOIN public.conversation_messages AS m "
                "ON m.message_id = j.boundary_message_id "
                "JOIN public.conversations AS c "
                "ON c.conversation_id = m.conversation_id "
                "WHERE r.conversation_id IS NULL AND r.event_id = j.event_id "
                "AND r.user_id = c.user_id"
            ).format(receipt_table)
        )

    cursor.execute(
        sql.SQL(
            "SELECT event_id::text FROM {} WHERE conversation_id IS NULL ORDER BY event_id"
        ).format(receipt_table)
    )
    unresolved = [row[0] for row in cursor.fetchall()]
    if unresolved:
        joined = ", ".join(unresolved)
        raise LongTermMemoryConfigurationError(
            f"cannot backfill formation receipt conversation_id for events [{joined}]"
        )
    cursor.execute(
        sql.SQL("ALTER TABLE {} ALTER COLUMN conversation_id SET NOT NULL").format(receipt_table)
    )


def _validate_formation_schema(cursor, schema_name: str, collection_name: str) -> None:
    receipt_table_name = formation_receipt_table_name(collection_name)
    cursor.execute(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema_name, receipt_table_name),
    )
    receipt_columns = {name: (data_type, nullable) for name, data_type, nullable in cursor}
    required_columns = {
        "event_id": ("uuid", "NO"),
        "user_id": ("text", "NO"),
        "conversation_id": ("uuid", "NO"),
        "result": ("jsonb", "NO"),
        "memory_count": ("integer", "NO"),
        "committed_at": ("timestamp with time zone", "NO"),
    }
    if any(receipt_columns.get(name) != contract for name, contract in required_columns.items()):
        raise LongTermMemoryConfigurationError

    cursor.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass(%s) AND contype = 'p'",
        (f"{schema_name}.{receipt_table_name}",),
    )
    if cursor.fetchone() != ("PRIMARY KEY (event_id)",):
        raise LongTermMemoryConfigurationError

    cursor.execute(
        "SELECT to_regclass(%s)",
        (f"{schema_name}.{collection_name}_formation_receipt_owner_idx",),
    )
    owner_index_registration = cursor.fetchone()
    if not owner_index_registration or owner_index_registration[0] is None:
        raise LongTermMemoryConfigurationError

    cursor.execute(
        "SELECT to_regclass(%s)",
        (f"{schema_name}.{collection_name}_formation_event_id_idx",),
    )
    index_registration = cursor.fetchone()
    if not index_registration or index_registration[0] is None:
        raise LongTermMemoryConfigurationError
