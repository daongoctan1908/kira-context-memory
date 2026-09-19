"""Authentication service policy tests without HTTP or PostgreSQL."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.application.services.auth import (
    AuthService,
    hash_csrf_token,
    hash_session_token,
    normalize_username,
    validate_password,
)
from app.domain.errors.auth import (
    InvalidCredentialsError,
    InvalidCsrfTokenError,
    InvalidSessionError,
    PasswordPolicyError,
)
from app.domain.models.auth import AuthSession, AuthUser
from app.domain.models.identity import AuthenticatedPrincipal

_NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)
_USER_ID = UUID("11111111-1111-4111-8111-111111111111")
_SESSION_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeHasher:
    def __init__(self) -> None:
        self.hash_calls: list[str] = []
        self.verify_calls: list[tuple[str, str]] = []

    def hash(self, password: str) -> str:
        self.hash_calls.append(password)
        return f"hash:{password}"

    def verify_and_update(self, password: str, password_hash: str) -> tuple[bool, str | None]:
        self.verify_calls.append((password, password_hash))
        return password_hash == f"hash:{password}", (
            "hash:upgraded" if password == "upgrade-password" else None
        )


class FakeStore:
    def __init__(self, user: AuthUser | None) -> None:
        self.user = user
        self.failure_calls = []
        self.success_calls = []
        self.created = []
        self.resolved: AuthSession | None = None
        self.revoked = []
        self.password_changes = []

    async def find_user_by_username(self, username: str):
        if self.user is not None and self.user.username == username:
            return self.user
        return None

    async def record_login_failure(self, user_id, **values):
        self.failure_calls.append((user_id, values))

    async def record_login_success(self, user_id, **values):
        self.success_calls.append((user_id, values))

    async def create_session(self, **values):
        self.created.append(values)

    async def resolve_session(self, token_hash, **values):
        self.resolve_call = (token_hash, values)
        return self.resolved

    async def revoke_session(self, token_hash, **values):
        self.revoked.append((token_hash, values))

    async def change_password_and_revoke_sessions(self, user_id, **values):
        self.password_changes.append((user_id, values))


def _user(**changes) -> AuthUser:
    values = {
        "user_id": _USER_ID,
        "username": "alice",
        "password_hash": "hash:correct-password",
        "enabled": True,
        "failed_login_count": 0,
        "locked_until": None,
    }
    values.update(changes)
    return AuthUser(**values)


def _service(store: FakeStore, hasher: FakeHasher | None = None) -> tuple[AuthService, FakeHasher]:
    resolved_hasher = hasher or FakeHasher()
    tokens = iter((b"s" * 32, b"c" * 32))
    service = AuthService(
        store,
        resolved_hasher,
        now=lambda: _NOW,
        token_bytes=lambda _length: next(tokens),
        session_id_factory=lambda: _SESSION_ID,
        dummy_password_hash="hash:dummy-password",
    )
    return service, resolved_hasher


async def test_login_normalizes_username_and_persists_only_token_hashes() -> None:
    store = FakeStore(_user())
    service, _ = _service(store)

    issued = await service.login("  ALICE ", "correct-password")

    assert issued.principal == AuthenticatedPrincipal(str(_USER_ID))
    assert issued.username == "alice"
    assert issued.absolute_expires_at == _NOW + timedelta(hours=8)
    assert store.success_calls[0][0] == _USER_ID
    created = store.created[0]
    assert created["session_id"] == _SESSION_ID
    assert created["token_hash"] == hash_session_token(issued.session_token)
    assert created["csrf_token_hash"] == hash_csrf_token(_SESSION_ID, issued.csrf_token)
    assert created["idle_expires_at"] == _NOW + timedelta(hours=2)
    assert "session_token" not in created and "csrf_token" not in created


async def test_login_rejects_unknown_disabled_locked_and_bad_password_uniformly() -> None:
    missing_store = FakeStore(None)
    missing, missing_hasher = _service(missing_store)
    with pytest.raises(InvalidCredentialsError):
        await missing.login("alice", "wrong-password")
    assert missing_hasher.verify_calls == [("wrong-password", "hash:dummy-password")]

    for user in (
        _user(enabled=False),
        _user(locked_until=_NOW + timedelta(minutes=1)),
    ):
        service, hasher = _service(FakeStore(user))
        with pytest.raises(InvalidCredentialsError):
            await service.login("alice", "correct-password")
        assert hasher.verify_calls[-1][0] == "correct-password"

    store = FakeStore(_user())
    service, _ = _service(store)
    with pytest.raises(InvalidCredentialsError):
        await service.login("alice", "wrong-password")
    assert len(store.failure_calls) == 1
    assert store.failure_calls[0][1]["threshold"] == 5
    assert store.failure_calls[0][1]["lock_duration"] == timedelta(minutes=15)


async def test_login_persists_pwdlib_rehash_when_verification_requests_it() -> None:
    store = FakeStore(_user(password_hash="hash:upgrade-password"))
    service, _ = _service(store)

    await service.login("alice", "upgrade-password")

    assert store.success_calls[0][1]["replacement_password_hash"] == "hash:upgraded"


async def test_resolve_csrf_logout_and_change_password_revoke_all_sessions() -> None:
    store = FakeStore(_user())
    service, hasher = _service(store)
    csrf = "csrf-value"
    store.resolved = AuthSession(
        session_id=_SESSION_ID,
        principal=AuthenticatedPrincipal(str(_USER_ID)),
        username="alice",
        csrf_token_hash=hash_csrf_token(_SESSION_ID, csrf),
        absolute_expires_at=_NOW + timedelta(hours=8),
    )

    resolved = await service.resolve("session-value")
    await service.verify_csrf(resolved, csrf)
    with pytest.raises(InvalidCsrfTokenError):
        await service.verify_csrf(resolved, "wrong-csrf")
    await service.logout("session-value")
    await service.change_password("session-value", csrf, "correct-password", "new-password-123")

    assert store.resolve_call[0] == hash_session_token("session-value")
    assert store.revoked[0][0] == hash_session_token("session-value")
    assert store.password_changes[0][0] == _USER_ID
    assert store.password_changes[0][1]["password_hash"] == "hash:new-password-123"
    assert "new-password-123" in hasher.hash_calls


async def test_invalid_session_password_and_configuration_fail_closed() -> None:
    store = FakeStore(_user())
    service, _ = _service(store)
    with pytest.raises(InvalidSessionError):
        await service.resolve("")
    with pytest.raises(InvalidSessionError):
        await service.resolve("unknown")
    await service.logout("")
    with pytest.raises(InvalidCredentialsError):
        normalize_username("bad username")
    with pytest.raises(PasswordPolicyError):
        validate_password("short")
    with pytest.raises(PasswordPolicyError):
        validate_password("x" * 129)
    with pytest.raises(ValueError, match="TTL"):
        AuthService(store, FakeHasher(), idle_ttl=timedelta(hours=9))
    with pytest.raises(ValueError, match="timezone-aware"):
        AuthService(
            store,
            FakeHasher(),
            now=lambda: datetime(2026, 9, 19),
            dummy_password_hash="hash:dummy",
        )._aware_now()
