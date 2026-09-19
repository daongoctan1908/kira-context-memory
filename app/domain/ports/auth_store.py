"""Persistence port for application-managed authentication."""

from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.domain.models.auth import AuthSession, AuthUser, AuthUserSummary


class AuthStorePort(Protocol):
    async def validate_schema(self) -> None: ...

    async def find_user_by_username(self, username: str) -> AuthUser | None: ...

    async def record_login_failure(
        self,
        user_id: UUID,
        *,
        now: datetime,
        threshold: int,
        lock_duration: timedelta,
    ) -> None: ...

    async def record_login_success(
        self,
        user_id: UUID,
        *,
        now: datetime,
        replacement_password_hash: str | None,
    ) -> None: ...

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
    ) -> None: ...

    async def resolve_session(
        self,
        token_hash: bytes,
        *,
        now: datetime,
        idle_ttl: timedelta,
        touch_interval: timedelta,
    ) -> AuthSession | None: ...

    async def revoke_session(self, token_hash: bytes, *, now: datetime) -> None: ...

    async def change_password_and_revoke_sessions(
        self,
        user_id: UUID,
        *,
        password_hash: str,
        now: datetime,
    ) -> None: ...

    async def create_user(
        self,
        *,
        user_id: UUID,
        username: str,
        password_hash: str,
        now: datetime,
    ) -> None: ...

    async def set_user_enabled(
        self,
        user_id: UUID,
        *,
        enabled: bool,
        now: datetime,
    ) -> None: ...

    async def revoke_user_sessions(self, user_id: UUID, *, now: datetime) -> int: ...

    async def list_users(self, *, limit: int) -> tuple[AuthUserSummary, ...]: ...
