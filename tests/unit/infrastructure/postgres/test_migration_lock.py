from collections.abc import Awaitable
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.postgres.migration_lock import (
    DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS,
    MIGRATION_ADVISORY_LOCK_ID,
    MigrationLockTimeoutError,
    acquire_migration_lock,
    migration_lock_timeout_seconds,
    release_migration_lock,
)


def test_migration_lock_timeout_has_bounded_default_and_validates_override() -> None:
    assert migration_lock_timeout_seconds({}) == DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS
    assert migration_lock_timeout_seconds({"MIGRATION_LOCK_TIMEOUT_SECONDS": "12.5"}) == 12.5

    for raw in ("", "zero", "0", "-1", "nan", "inf"):
        with pytest.raises(RuntimeError, match="positive number"):
            migration_lock_timeout_seconds({"MIGRATION_LOCK_TIMEOUT_SECONDS": raw})


@pytest.mark.asyncio
async def test_acquire_migration_lock_retries_without_logging_database_details() -> None:
    connection = AsyncMock()
    connection.scalar.side_effect = [False, True]
    pauses: list[float] = []

    async def pause(delay: float) -> None:
        pauses.append(delay)

    clock_values = iter((10.0, 10.1))
    await acquire_migration_lock(
        connection,
        timeout_seconds=5,
        clock=lambda: next(clock_values),
        pause=pause,
    )

    assert pauses == [0.25]
    assert connection.scalar.await_count == 2
    assert connection.scalar.await_args_list[0].args[1] == {"lock_id": MIGRATION_ADVISORY_LOCK_ID}


@pytest.mark.asyncio
async def test_acquire_migration_lock_fails_after_bounded_wait() -> None:
    connection = AsyncMock()
    connection.scalar.return_value = False
    pause = AsyncMock(spec=lambda delay: Awaitable[None])
    clock_values = iter((10.0, 11.0))

    with pytest.raises(MigrationLockTimeoutError, match="timed out"):
        await acquire_migration_lock(
            connection,
            timeout_seconds=0.5,
            clock=lambda: next(clock_values),
            pause=pause,
        )

    pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_migration_lock_requires_current_session_ownership() -> None:
    connection = AsyncMock()
    connection.scalar.return_value = True

    await release_migration_lock(connection)
    assert connection.scalar.await_args.args[1] == {"lock_id": MIGRATION_ADVISORY_LOCK_ID}

    connection.scalar.return_value = False
    with pytest.raises(RuntimeError, match="not held"):
        await release_migration_lock(connection)
