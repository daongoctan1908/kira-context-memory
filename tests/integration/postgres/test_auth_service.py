"""Real PostgreSQL acceptance for login, session expiry and password revocation."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.services.auth import AuthService, hash_session_token
from app.application.services.auth_admin import AuthAdminService
from app.domain.errors.auth import InvalidCredentialsError, InvalidSessionError
from app.infrastructure.auth import PwdlibPasswordHasher
from app.infrastructure.postgres.auth_store import PostgresAuthStore
from app.infrastructure.postgres.schema import auth_sessions, auth_users

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


async def test_real_login_lockout_resolve_and_password_change(engine) -> None:
    user_id = uuid4()
    username = f"auth.{user_id.hex[:8]}"
    hasher = PwdlibPasswordHasher()
    async with engine.begin() as connection:
        await connection.execute(
            insert(auth_users).values(
                user_id=user_id,
                username=username,
                password_hash=hasher.hash("initial-password-123"),
            )
        )
    service = AuthService(PostgresAuthStore(engine), hasher)
    try:
        for _ in range(4):
            with pytest.raises(InvalidCredentialsError):
                await service.login(username, "wrong-password")
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(auth_users.c.failed_login_count).where(auth_users.c.user_id == user_id)
                )
                == 4
            )

        issued = await service.login(username.upper(), "initial-password-123")
        resolved = await service.resolve(issued.session_token)
        await service.verify_csrf(resolved, issued.csrf_token)
        await service.change_password(
            issued.session_token,
            issued.csrf_token,
            "initial-password-123",
            "replacement-password-123",
        )
        with pytest.raises(InvalidSessionError):
            await service.resolve(issued.session_token)
        replacement = await service.login(username, "replacement-password-123")
        admin = AuthAdminService(PostgresAuthStore(engine), hasher)
        assert await admin.set_enabled(username, enabled=False)
        with pytest.raises(InvalidSessionError):
            await service.resolve(replacement.session_token)
        assert await admin.set_enabled(username, enabled=True)
        with pytest.raises(InvalidSessionError):
            await service.resolve(replacement.session_token)
        fresh = await service.login(username, "replacement-password-123")
        assert await admin.revoke_sessions(username) == 1
        with pytest.raises(InvalidSessionError):
            await service.resolve(fresh.session_token)
        async with engine.connect() as connection:
            revoked = await connection.scalar(
                select(auth_sessions.c.revoked_at).where(
                    auth_sessions.c.token_hash == hash_session_token(fresh.session_token)
                )
            )
        assert revoked is not None
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(auth_users).where(auth_users.c.user_id == user_id))
