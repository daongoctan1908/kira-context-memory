"""Trusted user administration service tests."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.services.auth_admin import AuthAdminService
from app.domain.models.auth import AuthUser, AuthUserSummary

_NOW = datetime(2026, 9, 19, tzinfo=UTC)
_USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeHasher:
    def hash(self, password: str) -> str:
        return f"hash:{password}"

    def verify_and_update(self, password: str, password_hash: str):
        return password_hash == f"hash:{password}", None


class FakeStore:
    def __init__(self, found=True) -> None:
        self.user = (
            AuthUser(_USER_ID, "alice", "hash:password-123", True, 0, None) if found else None
        )
        self.created = []
        self.enabled = []
        self.changed = []
        self.revoked = []
        self.users = (AuthUserSummary(_USER_ID, "alice", True, 0, None, _NOW, _NOW),)

    async def create_user(self, **values):
        self.created.append(values)

    async def find_user_by_username(self, username):
        return self.user if self.user and self.user.username == username else None

    async def set_user_enabled(self, user_id, **values):
        self.enabled.append((user_id, values))

    async def change_password_and_revoke_sessions(self, user_id, **values):
        self.changed.append((user_id, values))

    async def revoke_user_sessions(self, user_id, **values):
        self.revoked.append((user_id, values))
        return 2

    async def list_users(self, *, limit):
        self.limit = limit
        return self.users


def _service(store: FakeStore, *, now=lambda: _NOW) -> AuthAdminService:
    return AuthAdminService(
        store,  # type: ignore[arg-type]
        FakeHasher(),
        now=now,
        user_id_factory=lambda: _USER_ID,
    )


async def test_create_normalizes_username_and_hashes_password() -> None:
    store = FakeStore()
    user_id = await _service(store).create_user(" ALICE ", "password-123")

    assert user_id == _USER_ID
    assert store.created == [
        {
            "user_id": _USER_ID,
            "username": "alice",
            "password_hash": "hash:password-123",
            "now": _NOW,
        }
    ]


async def test_enable_disable_reset_revoke_and_list_handle_missing_user() -> None:
    store = FakeStore()
    service = _service(store)

    assert await service.set_enabled("alice", enabled=False)
    assert await service.reset_password("alice", "replacement-123")
    assert await service.revoke_sessions("alice") == 2
    assert await service.list_users(limit=50) == store.users
    assert store.enabled[0] == (_USER_ID, {"enabled": False, "now": _NOW})
    assert store.changed[0][1]["password_hash"] == "hash:replacement-123"
    assert store.revoked[0][0] == _USER_ID
    assert store.limit == 50

    missing = _service(FakeStore(found=False))
    assert not await missing.set_enabled("alice", enabled=True)
    assert not await missing.reset_password("alice", "replacement-123")
    assert await missing.revoke_sessions("alice") is None


async def test_admin_clock_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await _service(FakeStore(), now=lambda: datetime(2026, 9, 19)).create_user(
            "alice", "password-123"
        )
