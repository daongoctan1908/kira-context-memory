"""Authentication records shared by application services and storage adapters."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.models.identity import AuthenticatedPrincipal


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AuthUser:
    user_id: UUID
    username: str
    password_hash: str = field(repr=False)
    enabled: bool
    failed_login_count: int
    locked_until: datetime | None

    def __post_init__(self) -> None:
        if not self.username or self.username != self.username.lower():
            raise ValueError("username must be normalized")
        if not self.password_hash:
            raise ValueError("password hash must not be empty")
        if self.failed_login_count < 0:
            raise ValueError("failed login count must not be negative")
        if self.locked_until is not None:
            _aware(self.locked_until, "locked_until")


@dataclass(frozen=True, slots=True)
class AuthSession:
    session_id: UUID
    principal: AuthenticatedPrincipal
    username: str
    csrf_token_hash: bytes = field(repr=False)
    absolute_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.username:
            raise ValueError("username must not be empty")
        if len(self.csrf_token_hash) != 32:
            raise ValueError("CSRF token hash must be 32 bytes")
        _aware(self.absolute_expires_at, "absolute_expires_at")


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_id: UUID
    principal: AuthenticatedPrincipal
    username: str
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    absolute_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.username or not self.session_token or not self.csrf_token:
            raise ValueError("issued session fields must not be empty")
        _aware(self.absolute_expires_at, "absolute_expires_at")
