"""PostgreSQL persistence for users and opaque authentication sessions."""

from builtins import TimeoutError as BuiltinTimeoutError
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.errors.auth import AuthConflictError, AuthStoreError
from app.domain.models.auth import AuthSession, AuthUser, AuthUserSummary
from app.domain.models.identity import AuthenticatedPrincipal
from app.infrastructure.postgres.schema import (
    SUPPORTED_SCHEMA_REVISIONS,
    auth_sessions,
    auth_users,
)


class PostgresAuthStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def validate_schema(self) -> None:
        try:
            async with self._engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        if revision not in SUPPORTED_SCHEMA_REVISIONS:
            raise AuthStoreError from None

    async def find_user_by_username(self, username: str) -> AuthUser | None:
        statement = select(auth_users).where(auth_users.c.username == username)
        try:
            async with self._engine.connect() as connection:
                row = (await connection.execute(statement)).mappings().one_or_none()
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        return self._user(row) if row is not None else None

    async def record_login_failure(
        self,
        user_id: UUID,
        *,
        now: datetime,
        threshold: int,
        lock_duration: timedelta,
    ) -> None:
        try:
            async with self._engine.begin() as connection:
                row = (
                    (
                        await connection.execute(
                            select(
                                auth_users.c.failed_login_count,
                                auth_users.c.locked_until,
                            )
                            .where(auth_users.c.user_id == user_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return
                previous = (
                    0
                    if row["locked_until"] is not None and row["locked_until"] <= now
                    else row["failed_login_count"]
                )
                count = previous + 1
                values: dict[str, object] = {
                    "failed_login_count": count,
                    "updated_at": now,
                }
                if count >= threshold:
                    values["locked_until"] = now + lock_duration
                await connection.execute(
                    update(auth_users).where(auth_users.c.user_id == user_id).values(**values)
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def record_login_success(
        self,
        user_id: UUID,
        *,
        now: datetime,
        replacement_password_hash: str | None,
    ) -> None:
        values: dict[str, object] = {
            "failed_login_count": 0,
            "locked_until": None,
            "updated_at": now,
        }
        if replacement_password_hash is not None:
            values["password_hash"] = replacement_password_hash
            values["password_changed_at"] = now
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    update(auth_users).where(auth_users.c.user_id == user_id).values(**values)
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def create_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        token_hash: bytes,
        csrf_token_hash: bytes,
        now: datetime,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
    ) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    insert(auth_sessions).values(
                        session_id=session_id,
                        user_id=user_id,
                        token_hash=token_hash,
                        csrf_token_hash=csrf_token_hash,
                        created_at=now,
                        last_seen_at=now,
                        idle_expires_at=idle_expires_at,
                        absolute_expires_at=absolute_expires_at,
                    )
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def resolve_session(
        self,
        token_hash: bytes,
        *,
        now: datetime,
        idle_ttl: timedelta,
        touch_interval: timedelta,
    ) -> AuthSession | None:
        statement = (
            select(
                auth_sessions.c.session_id,
                auth_sessions.c.csrf_token_hash,
                auth_sessions.c.last_seen_at,
                auth_sessions.c.idle_expires_at,
                auth_sessions.c.absolute_expires_at,
                auth_sessions.c.revoked_at,
                auth_users.c.user_id,
                auth_users.c.username,
                auth_users.c.enabled,
            )
            .select_from(
                auth_sessions.join(auth_users, auth_sessions.c.user_id == auth_users.c.user_id)
            )
            .where(auth_sessions.c.token_hash == token_hash)
            .with_for_update(of=auth_sessions)
        )
        try:
            async with self._engine.begin() as connection:
                row = (await connection.execute(statement)).mappings().one_or_none()
                if row is None or not self._session_is_active(row, now):
                    return None
                if row["last_seen_at"] + touch_interval <= now:
                    idle_expires_at = min(now + idle_ttl, row["absolute_expires_at"])
                    await connection.execute(
                        update(auth_sessions)
                        .where(auth_sessions.c.session_id == row["session_id"])
                        .values(last_seen_at=now, idle_expires_at=idle_expires_at)
                    )
                return AuthSession(
                    session_id=row["session_id"],
                    principal=AuthenticatedPrincipal(str(row["user_id"])),
                    username=row["username"],
                    csrf_token_hash=bytes(row["csrf_token_hash"]),
                    absolute_expires_at=row["absolute_expires_at"],
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def revoke_session(self, token_hash: bytes, *, now: datetime) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    update(auth_sessions)
                    .where(
                        auth_sessions.c.token_hash == token_hash,
                        auth_sessions.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def change_password_and_revoke_sessions(
        self,
        user_id: UUID,
        *,
        password_hash: str,
        now: datetime,
    ) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    update(auth_users)
                    .where(auth_users.c.user_id == user_id)
                    .values(
                        password_hash=password_hash,
                        password_changed_at=now,
                        failed_login_count=0,
                        locked_until=None,
                        updated_at=now,
                    )
                )
                await connection.execute(
                    update(auth_sessions)
                    .where(
                        auth_sessions.c.user_id == user_id,
                        auth_sessions.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def create_user(
        self,
        *,
        user_id: UUID,
        username: str,
        password_hash: str,
        now: datetime,
    ) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    insert(auth_users).values(
                        user_id=user_id,
                        username=username,
                        password_hash=password_hash,
                        created_at=now,
                        updated_at=now,
                        password_changed_at=now,
                    )
                )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def set_user_enabled(
        self,
        user_id: UUID,
        *,
        enabled: bool,
        now: datetime,
    ) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    update(auth_users)
                    .where(auth_users.c.user_id == user_id)
                    .values(enabled=enabled, updated_at=now)
                )
                if not enabled:
                    await connection.execute(
                        update(auth_sessions)
                        .where(
                            auth_sessions.c.user_id == user_id,
                            auth_sessions.c.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def revoke_user_sessions(self, user_id: UUID, *, now: datetime) -> int:
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(
                    update(auth_sessions)
                    .where(
                        auth_sessions.c.user_id == user_id,
                        auth_sessions.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                return max(result.rowcount or 0, 0)
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)

    async def list_users(self, *, limit: int) -> tuple[AuthUserSummary, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("auth user list limit must be between 1 and 1000")
        statement = (
            select(
                auth_users.c.user_id,
                auth_users.c.username,
                auth_users.c.enabled,
                auth_users.c.failed_login_count,
                auth_users.c.locked_until,
                auth_users.c.created_at,
                auth_users.c.password_changed_at,
            )
            .order_by(auth_users.c.username.asc(), auth_users.c.user_id.asc())
            .limit(limit)
        )
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).mappings().all()
        except (BuiltinTimeoutError, OSError, SQLAlchemyError) as error:
            self._raise_mapped(error)
        return tuple(AuthUserSummary(**row) for row in rows)

    @staticmethod
    def _session_is_active(row, now: datetime) -> bool:
        return bool(
            row["enabled"]
            and row["revoked_at"] is None
            and row["idle_expires_at"] > now
            and row["absolute_expires_at"] > now
        )

    @staticmethod
    def _user(row) -> AuthUser:
        return AuthUser(
            user_id=row["user_id"],
            username=row["username"],
            password_hash=row["password_hash"],
            enabled=row["enabled"],
            failed_login_count=row["failed_login_count"],
            locked_until=row["locked_until"],
        )

    @staticmethod
    def _raise_mapped(error: BaseException) -> None:
        if isinstance(error, IntegrityError):
            raise AuthConflictError from None
        if isinstance(error, DBAPIError) and error.connection_invalidated:
            raise AuthStoreError from None
        raise AuthStoreError from None
