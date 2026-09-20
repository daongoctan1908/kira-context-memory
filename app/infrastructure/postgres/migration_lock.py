"""Bounded PostgreSQL advisory lock used to serialize Alembic upgrades."""

import asyncio
import math
import os
from collections.abc import Awaitable, Callable, Mapping
from time import monotonic

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

MIGRATION_ADVISORY_LOCK_ID = int.from_bytes(b"KIRAMIGR", byteorder="big", signed=False)
DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS = 60.0
MIGRATION_LOCK_POLL_SECONDS = 0.25


class MigrationLockTimeoutError(RuntimeError):
    """Raised when another migration process keeps the database lock too long."""


def migration_lock_timeout_seconds(
    environment: Mapping[str, str] | None = None,
) -> float:
    """Read and validate the bounded migration-lock timeout."""
    source = os.environ if environment is None else environment
    raw = source.get(
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        str(DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("MIGRATION_LOCK_TIMEOUT_SECONDS must be a positive number") from error
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("MIGRATION_LOCK_TIMEOUT_SECONDS must be a positive number")
    return value


async def acquire_migration_lock(
    connection: AsyncConnection,
    *,
    timeout_seconds: float,
    clock: Callable[[], float] = monotonic,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Acquire the session lock without leaving a migration process blocked forever."""
    deadline = clock() + timeout_seconds
    statement = text("SELECT pg_try_advisory_lock(:lock_id)")
    while True:
        acquired = await connection.scalar(
            statement,
            {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
        )
        if acquired is True:
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise MigrationLockTimeoutError(
                "timed out waiting for the PostgreSQL migration advisory lock"
            )
        await pause(min(MIGRATION_LOCK_POLL_SECONDS, remaining))


async def release_migration_lock(connection: AsyncConnection) -> None:
    """Release the session lock held by the current migration connection."""
    released = await connection.scalar(
        text("SELECT pg_advisory_unlock(:lock_id)"),
        {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
    )
    if released is not True:
        raise RuntimeError("PostgreSQL migration advisory lock was not held by this session")
