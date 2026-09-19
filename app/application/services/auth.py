"""Authentication orchestration independent of HTTP cookies and PostgreSQL."""

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from app.domain.errors.auth import (
    InvalidCredentialsError,
    InvalidCsrfTokenError,
    InvalidSessionError,
    PasswordPolicyError,
)
from app.domain.models.auth import AuthSession, IssuedSession
from app.domain.models.identity import AuthenticatedPrincipal
from app.domain.ports.auth_store import AuthStorePort

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


class PasswordHasherPort(Protocol):
    def hash(self, password: str) -> str: ...

    def verify_and_update(self, password: str, password_hash: str) -> tuple[bool, str | None]: ...


class AuthService:
    """Issue and verify opaque sessions while keeping raw secrets out of persistence."""

    def __init__(
        self,
        store: AuthStorePort,
        password_hasher: PasswordHasherPort,
        *,
        now: Callable[[], datetime] | None = None,
        token_bytes: Callable[[int], bytes] | None = None,
        session_id_factory: Callable[[], UUID] = uuid4,
        idle_ttl: timedelta = timedelta(hours=2),
        absolute_ttl: timedelta = timedelta(hours=8),
        touch_interval: timedelta = timedelta(minutes=5),
        lock_threshold: int = 5,
        lock_duration: timedelta = timedelta(minutes=15),
        dummy_password_hash: str | None = None,
    ) -> None:
        if idle_ttl <= timedelta(0) or absolute_ttl < idle_ttl:
            raise ValueError("auth session TTLs are invalid")
        if touch_interval <= timedelta(0) or touch_interval > idle_ttl:
            raise ValueError("auth session touch interval is invalid")
        if lock_threshold < 1 or lock_duration <= timedelta(0):
            raise ValueError("auth lockout settings are invalid")
        self._store = store
        self._password_hasher = password_hasher
        self._now = now or (lambda: datetime.now(UTC))
        self._token_bytes = token_bytes or secrets.token_bytes
        self._session_id_factory = session_id_factory
        self._idle_ttl = idle_ttl
        self._absolute_ttl = absolute_ttl
        self._touch_interval = touch_interval
        self._lock_threshold = lock_threshold
        self._lock_duration = lock_duration
        self._dummy_password_hash = dummy_password_hash or password_hasher.hash(
            "not-a-real-user-password"
        )

    async def login(self, username: str, password: str) -> IssuedSession:
        normalized = normalize_username(username)
        now = self._aware_now()
        user = await self._store.find_user_by_username(normalized)
        if user is None:
            self._password_hasher.verify_and_update(password, self._dummy_password_hash)
            raise InvalidCredentialsError

        valid, replacement = self._password_hasher.verify_and_update(password, user.password_hash)
        if not user.enabled or (user.locked_until is not None and user.locked_until > now):
            raise InvalidCredentialsError
        if not valid:
            await self._store.record_login_failure(
                user.user_id,
                now=now,
                threshold=self._lock_threshold,
                lock_duration=self._lock_duration,
            )
            raise InvalidCredentialsError

        await self._store.record_login_success(
            user.user_id,
            now=now,
            replacement_password_hash=replacement,
        )
        session_id = self._session_id_factory()
        session_token = _encode_token(self._token_bytes(32))
        csrf_token = _encode_token(self._token_bytes(32))
        absolute_expires_at = now + self._absolute_ttl
        await self._store.create_session(
            session_id=session_id,
            user_id=user.user_id,
            token_hash=hash_session_token(session_token),
            csrf_token_hash=hash_csrf_token(session_id, csrf_token),
            now=now,
            idle_expires_at=now + self._idle_ttl,
            absolute_expires_at=absolute_expires_at,
        )
        return IssuedSession(
            session_id=session_id,
            principal=AuthenticatedPrincipal(str(user.user_id)),
            username=user.username,
            session_token=session_token,
            csrf_token=csrf_token,
            absolute_expires_at=absolute_expires_at,
        )

    async def resolve(self, session_token: str) -> AuthSession:
        if not session_token:
            raise InvalidSessionError
        session = await self._store.resolve_session(
            hash_session_token(session_token),
            now=self._aware_now(),
            idle_ttl=self._idle_ttl,
            touch_interval=self._touch_interval,
        )
        if session is None:
            raise InvalidSessionError
        return session

    async def verify_csrf(self, session: AuthSession, csrf_token: str) -> None:
        candidate = hash_csrf_token(session.session_id, csrf_token)
        if not hmac.compare_digest(candidate, session.csrf_token_hash):
            raise InvalidCsrfTokenError

    async def logout(self, session_token: str) -> None:
        if session_token:
            await self._store.revoke_session(
                hash_session_token(session_token),
                now=self._aware_now(),
            )

    async def change_password(
        self,
        session_token: str,
        csrf_token: str,
        current_password: str,
        new_password: str,
    ) -> None:
        session = await self.resolve(session_token)
        await self.verify_csrf(session, csrf_token)
        user = await self._store.find_user_by_username(session.username)
        if user is None or user.user_id != UUID(session.principal.user_id):
            raise InvalidSessionError
        valid, _ = self._password_hasher.verify_and_update(current_password, user.password_hash)
        if not valid:
            raise InvalidCredentialsError
        validate_password(new_password)
        password_hash = self._password_hasher.hash(new_password)
        await self._store.change_password_and_revoke_sessions(
            user.user_id,
            password_hash=password_hash,
            now=self._aware_now(),
        )

    def hash_password(self, password: str) -> str:
        """Validate and hash a password for trusted administration flows."""
        validate_password(password)
        return self._password_hasher.hash(password)

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("auth clock must return a timezone-aware timestamp")
        return value


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise InvalidCredentialsError
    return normalized


def validate_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError


def hash_session_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def hash_csrf_token(session_id: UUID, token: str) -> bytes:
    return hashlib.sha256(session_id.bytes + b"\0" + token.encode("utf-8")).digest()


def _encode_token(value: bytes) -> str:
    if len(value) != 32:
        raise ValueError("auth token source must return exactly 32 bytes")
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
