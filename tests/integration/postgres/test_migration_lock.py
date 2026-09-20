"""PostgreSQL semantics for the bounded Alembic advisory lock."""

import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.postgres.migration_lock import (
    MigrationLockTimeoutError,
    acquire_migration_lock,
    release_migration_lock,
)

pytestmark = pytest.mark.postgres_integration


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


@pytest.mark.asyncio
async def test_migration_lock_serializes_sessions_and_can_be_reacquired() -> None:
    engine = create_async_engine(_test_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as owner, engine.connect() as contender:
            await acquire_migration_lock(owner, timeout_seconds=1)
            await owner.commit()

            with pytest.raises(MigrationLockTimeoutError):
                await acquire_migration_lock(contender, timeout_seconds=0.05)
            await contender.rollback()

            await release_migration_lock(owner)
            await owner.commit()

            await acquire_migration_lock(contender, timeout_seconds=1)
            await contender.commit()
            await release_migration_lock(contender)
            await contender.commit()
    finally:
        await engine.dispose()
