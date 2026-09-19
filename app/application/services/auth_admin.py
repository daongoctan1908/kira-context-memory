"""Trusted administration operations for application-managed users."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.application.services.auth import PasswordHasherPort, normalize_username, validate_password
from app.domain.models.auth import AuthUserSummary
from app.domain.ports.auth_store import AuthStorePort


class AuthAdminService:
    def __init__(
        self,
        store: AuthStorePort,
        password_hasher: PasswordHasherPort,
        *,
        now: Callable[[], datetime] | None = None,
        user_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._password_hasher = password_hasher
        self._now = now or (lambda: datetime.now(UTC))
        self._user_id_factory = user_id_factory

    async def create_user(self, username: str, password: str) -> UUID:
        normalized = normalize_username(username)
        validate_password(password)
        user_id = self._user_id_factory()
        await self._store.create_user(
            user_id=user_id,
            username=normalized,
            password_hash=self._password_hasher.hash(password),
            now=self._aware_now(),
        )
        return user_id

    async def set_enabled(self, username: str, *, enabled: bool) -> bool:
        user = await self._store.find_user_by_username(normalize_username(username))
        if user is None:
            return False
        await self._store.set_user_enabled(user.user_id, enabled=enabled, now=self._aware_now())
        return True

    async def reset_password(self, username: str, password: str) -> bool:
        normalized = normalize_username(username)
        validate_password(password)
        user = await self._store.find_user_by_username(normalized)
        if user is None:
            return False
        await self._store.change_password_and_revoke_sessions(
            user.user_id,
            password_hash=self._password_hasher.hash(password),
            now=self._aware_now(),
        )
        return True

    async def revoke_sessions(self, username: str) -> int | None:
        user = await self._store.find_user_by_username(normalize_username(username))
        if user is None:
            return None
        return await self._store.revoke_user_sessions(user.user_id, now=self._aware_now())

    async def list_users(self, *, limit: int) -> tuple[AuthUserSummary, ...]:
        return await self._store.list_users(limit=limit)

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("auth admin clock must return a timezone-aware timestamp")
        return value
