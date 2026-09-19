"""PostgreSQL migration checks for users and opaque auth sessions."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    auth_sessions,
    auth_users,
)

pytestmark = pytest.mark.postgres_integration


def _test_url() -> str:
    value = os.environ.get("POSTGRES_TEST_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


@pytest.fixture(scope="module")
def migrated_database() -> str:
    database_url = _test_url()
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    return database_url


@pytest.fixture
async def engine(migrated_database: str):
    value = create_async_engine(migrated_database, pool_pre_ping=True)
    try:
        yield value
    finally:
        await value.dispose()


async def test_auth_migration_creates_expected_revision_and_hash_only_session(engine) -> None:
    user_id = uuid4()
    session_id = uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        await connection.execute(
            insert(auth_users).values(
                user_id=user_id,
                username=f"user.{user_id.hex[:8]}",
                password_hash="$argon2id$synthetic",
            )
        )
        await connection.execute(
            insert(auth_sessions).values(
                session_id=session_id,
                user_id=user_id,
                token_hash=b"t" * 32,
                csrf_token_hash=b"c" * 32,
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=2),
                absolute_expires_at=now + timedelta(hours=8),
            )
        )
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(auth_sessions).where(auth_sessions.c.session_id == session_id)
                )
            )
            .mappings()
            .one()
        )
    assert revision == EXPECTED_SCHEMA_REVISION
    assert row["token_hash"] == b"t" * 32
    assert row["csrf_token_hash"] == b"c" * 32
    async with engine.begin() as connection:
        await connection.execute(delete(auth_users).where(auth_users.c.user_id == user_id))


async def test_auth_database_rejects_non_normalized_username_and_bad_hash(engine) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                insert(auth_users).values(
                    user_id=uuid4(), username="MixedCase", password_hash="$argon2id$synthetic"
                )
            )

    user_id = uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            insert(auth_users).values(
                user_id=user_id,
                username=f"user.{user_id.hex[:8]}",
                password_hash="$argon2id$synthetic",
            )
        )
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    insert(auth_sessions).values(
                        session_id=uuid4(),
                        user_id=user_id,
                        token_hash=b"short",
                        csrf_token_hash=b"c" * 32,
                        created_at=now,
                        last_seen_at=now,
                        idle_expires_at=now + timedelta(hours=2),
                        absolute_expires_at=now + timedelta(hours=8),
                    )
                )
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(auth_users).where(auth_users.c.user_id == user_id))
