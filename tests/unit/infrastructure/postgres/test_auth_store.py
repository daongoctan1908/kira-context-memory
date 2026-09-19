"""PostgreSQL auth-store behavior without a live database."""

from collections import deque
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.domain.errors.auth import AuthConflictError, AuthStoreError
from app.infrastructure.postgres.auth_store import PostgresAuthStore

_NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)
_USER_ID = UUID("11111111-1111-4111-8111-111111111111")
_SESSION_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeResult:
    def __init__(self, row=None) -> None:
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class FakeConnection:
    def __init__(self, results=()) -> None:
        self.results = deque(results)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        self.calls.append(statement)
        result = self.results.popleft() if self.results else FakeResult()
        if isinstance(result, BaseException):
            raise result
        return result


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self):
        return self.connection

    def begin(self):
        return self.connection


def _store(connection: FakeConnection) -> PostgresAuthStore:
    return PostgresAuthStore(FakeEngine(connection))  # type: ignore[arg-type]


def _user_row(**changes):
    row = {
        "user_id": _USER_ID,
        "username": "alice",
        "password_hash": "hash-value",
        "enabled": True,
        "failed_login_count": 0,
        "locked_until": None,
    }
    row.update(changes)
    return row


def _session_row(**changes):
    row = {
        "session_id": _SESSION_ID,
        "csrf_token_hash": b"c" * 32,
        "last_seen_at": _NOW - timedelta(minutes=10),
        "idle_expires_at": _NOW + timedelta(hours=1),
        "absolute_expires_at": _NOW + timedelta(hours=6),
        "revoked_at": None,
        "user_id": _USER_ID,
        "username": "alice",
        "enabled": True,
    }
    row.update(changes)
    return row


async def test_find_user_and_missing_user_are_typed() -> None:
    found = await _store(FakeConnection((FakeResult(_user_row()),))).find_user_by_username("alice")
    missing = await _store(FakeConnection((FakeResult(),))).find_user_by_username("missing")

    assert found is not None
    assert found.user_id == _USER_ID
    assert found.password_hash == "hash-value"
    assert missing is None


async def test_login_failure_is_locked_and_expired_window_restarts_count() -> None:
    connection = FakeConnection(
        (
            FakeResult(_user_row(failed_login_count=4)),
            FakeResult(),
        )
    )
    await _store(connection).record_login_failure(
        _USER_ID,
        now=_NOW,
        threshold=5,
        lock_duration=timedelta(minutes=15),
    )
    values = connection.calls[1].compile().params
    assert values["failed_login_count"] == 5
    assert values["locked_until"] == _NOW + timedelta(minutes=15)

    expired = FakeConnection(
        (
            FakeResult(
                _user_row(
                    failed_login_count=5,
                    locked_until=_NOW - timedelta(seconds=1),
                )
            ),
            FakeResult(),
        )
    )
    await _store(expired).record_login_failure(
        _USER_ID,
        now=_NOW,
        threshold=5,
        lock_duration=timedelta(minutes=15),
    )
    assert expired.calls[1].compile().params["failed_login_count"] == 1


async def test_success_session_create_revoke_and_password_change_use_transactions() -> None:
    success = FakeConnection((FakeResult(),))
    await _store(success).record_login_success(
        _USER_ID,
        now=_NOW,
        replacement_password_hash="replacement",
    )
    success_values = success.calls[0].compile().params
    assert success_values["password_hash"] == "replacement"
    assert success_values["failed_login_count"] == 0

    created = FakeConnection((FakeResult(),))
    await _store(created).create_session(
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        token_hash=b"t" * 32,
        csrf_token_hash=b"c" * 32,
        now=_NOW,
        idle_expires_at=_NOW + timedelta(hours=2),
        absolute_expires_at=_NOW + timedelta(hours=8),
    )
    create_values = created.calls[0].compile().params
    assert create_values["token_hash"] == b"t" * 32
    assert create_values["csrf_token_hash"] == b"c" * 32

    revoked = FakeConnection((FakeResult(),))
    await _store(revoked).revoke_session(b"t" * 32, now=_NOW)
    assert revoked.calls[0].compile().params["revoked_at"] == _NOW

    changed = FakeConnection((FakeResult(), FakeResult()))
    await _store(changed).change_password_and_revoke_sessions(
        _USER_ID,
        password_hash="new-hash",
        now=_NOW,
    )
    assert changed.calls[0].compile().params["password_hash"] == "new-hash"
    assert changed.calls[1].compile().params["revoked_at"] == _NOW


async def test_resolve_session_checks_state_and_touches_at_most_every_five_minutes() -> None:
    touched = FakeConnection((FakeResult(_session_row()), FakeResult()))
    session = await _store(touched).resolve_session(
        b"t" * 32,
        now=_NOW,
        idle_ttl=timedelta(hours=2),
        touch_interval=timedelta(minutes=5),
    )
    assert session is not None
    assert session.principal.user_id == str(_USER_ID)
    assert len(touched.calls) == 2
    touch_values = touched.calls[1].compile().params
    assert touch_values["last_seen_at"] == _NOW
    assert touch_values["idle_expires_at"] == _NOW + timedelta(hours=2)

    recent = FakeConnection((FakeResult(_session_row(last_seen_at=_NOW - timedelta(minutes=1))),))
    assert (
        await _store(recent).resolve_session(
            b"t" * 32,
            now=_NOW,
            idle_ttl=timedelta(hours=2),
            touch_interval=timedelta(minutes=5),
        )
        is not None
    )
    assert len(recent.calls) == 1

    for changes in (
        {"enabled": False},
        {"revoked_at": _NOW},
        {"idle_expires_at": _NOW},
        {"absolute_expires_at": _NOW},
    ):
        inactive = FakeConnection((FakeResult(_session_row(**changes)),))
        assert (
            await _store(inactive).resolve_session(
                b"t" * 32,
                now=_NOW,
                idle_ttl=timedelta(hours=2),
                touch_interval=timedelta(minutes=5),
            )
            is None
        )


async def test_store_maps_integrity_and_database_errors_without_messages() -> None:
    integrity = IntegrityError("statement", {}, Exception("secret"))
    with pytest.raises(AuthConflictError) as conflict:
        await _store(FakeConnection((integrity,))).create_session(
            session_id=uuid4(),
            user_id=_USER_ID,
            token_hash=b"t" * 32,
            csrf_token_hash=b"c" * 32,
            now=_NOW,
            idle_expires_at=_NOW + timedelta(hours=2),
            absolute_expires_at=_NOW + timedelta(hours=8),
        )
    assert str(conflict.value) == ""

    with pytest.raises(AuthStoreError) as unavailable:
        await _store(FakeConnection((SQLAlchemyError("secret"),))).find_user_by_username("alice")
    assert str(unavailable.value) == ""
