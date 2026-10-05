"""Read-only database capability probes; no schema creation or corpus writes."""

import psycopg
from psycopg import sql

from evaluation.config import EvalConfig
from evaluation.errors import PreflightError, ProtocolError
from evaluation.models import (
    HISTORICAL_CONTROL_SHA,
    Outcome,
    Probe,
    Reason,
    RunProvenance,
)


def _expected_schema_contract(provenance: RunProvenance | None) -> tuple[str, int, str]:
    """Return the (alembic, memory schema_version, mem0 contract) pinned per runtime.

    The historical control predates both the conversation-management migration and
    memory schema 3, while every newer runtime requires them; the preflight probe
    must match the exact runtime being evaluated instead of one fixed snapshot.
    """
    if provenance is not None and provenance.runtime.sha == HISTORICAL_CONTROL_SHA:
        return ("20260908_0003", 2, "2.0.20+viettel.3")
    return ("20260920_0008", 3, "2.0.20+viettel.6")


async def probe_database(
    config: EvalConfig,
    probe: Probe,
    dimension: int | None = None,
    provenance: RunProvenance | None = None,
) -> dict:
    secret = config.database_url if probe == Probe.CONVERSATION_DB else config.memory_database_url
    assert secret is not None
    dsn = secret.get_secret_value().replace("postgresql+asyncpg://", "postgresql://", 1)
    options = "-c default_transaction_read_only=on -c statement_timeout=2000"
    expected_alembic, expected_schema_version, expected_mem0 = _expected_schema_contract(provenance)
    try:
        async with await psycopg.AsyncConnection.connect(
            dsn,
            autocommit=True,
            options=options,
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                if await cursor.fetchone() != (1,):
                    raise ProtocolError(Reason.DATABASE)
                if probe == Probe.CONVERSATION_DB:
                    await cursor.execute("SELECT version_num FROM public.alembic_version")
                    if await cursor.fetchall() != [(expected_alembic,)]:
                        raise ProtocolError(Reason.SCHEMA_MISMATCH)
                    for table in ("conversations", "conversation_messages", "memory_jobs"):
                        await cursor.execute(
                            sql.SQL("SELECT 1 FROM {} LIMIT 0").format(
                                sql.Identifier("public", table)
                            )
                        )
                else:
                    await cursor.execute(
                        "SELECT extversion FROM pg_extension WHERE extname = %s", ("vector",)
                    )
                    if not await cursor.fetchone():
                        raise ProtocolError(Reason.EXTENSION_MISSING)
                    if probe == Probe.MEMORY_SCHEMA:
                        await cursor.execute(
                            sql.SQL(
                                "SELECT schema_version, mem0_version, embedding_model, "
                                "embedding_dims "
                                "FROM {} WHERE singleton"
                            ).format(sql.Identifier(config.memory_schema, "kira_memory_schema"))
                        )
                        expected = (
                            expected_schema_version,
                            expected_mem0,
                            config.embedding.model,
                            dimension,
                        )
                        if await cursor.fetchall() != [expected]:
                            raise ProtocolError(Reason.SCHEMA_MISMATCH)
                        for table in (
                            config.memory_collection,
                            config.memory_collection + "_formation_receipts",
                        ):
                            await cursor.execute(
                                sql.SQL("SELECT 1 FROM {} LIMIT 0").format(
                                    sql.Identifier(config.memory_schema, table)
                                )
                            )
    except psycopg.errors.QueryCanceled:
        raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.TIMEOUT) from None
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        raise ProtocolError(Reason.SCHEMA_MISMATCH) from None
    except (psycopg.OperationalError, OSError):
        raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.CONNECTION) from None
    except (psycopg.Error, ValueError):
        raise PreflightError(Outcome.DEPENDENCY_ERROR, Reason.DATABASE) from None
    return {}
