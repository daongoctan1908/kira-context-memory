"""Static contract tests for application-managed authentication tables."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.infrastructure.postgres.schema import (
    EXPECTED_SCHEMA_REVISION,
    PREVIOUS_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISIONS,
    TELEMETRY_CONTEXT_SCHEMA_REVISIONS,
    auth_sessions,
    auth_users,
)


def test_auth_schema_advances_one_additive_bridge_revision() -> None:
    assert PREVIOUS_SCHEMA_REVISION == "20260919_0005"
    assert EXPECTED_SCHEMA_REVISION == "20260919_0006"
    assert SUPPORTED_SCHEMA_REVISIONS == {
        PREVIOUS_SCHEMA_REVISION,
        EXPECTED_SCHEMA_REVISION,
    }
    assert TELEMETRY_CONTEXT_SCHEMA_REVISIONS == SUPPORTED_SCHEMA_REVISIONS


def test_auth_users_schema_has_normalized_unique_accounts_and_lockout_state() -> None:
    assert list(auth_users.c.keys()) == [
        "user_id",
        "username",
        "password_hash",
        "enabled",
        "failed_login_count",
        "locked_until",
        "created_at",
        "updated_at",
        "password_changed_at",
    ]
    checks = {
        constraint.name
        for constraint in auth_users.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_auth_users_failed_login_count",
        "ck_auth_users_locked_until",
        "ck_auth_users_password_hash_nonempty",
        "ck_auth_users_username_normalized",
    }
    unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in auth_users.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique == {"uq_auth_users_username": ("username",)}


def test_auth_sessions_schema_uses_only_hashes_and_bounded_expiry() -> None:
    assert list(auth_sessions.c.keys()) == [
        "session_id",
        "user_id",
        "token_hash",
        "csrf_token_hash",
        "created_at",
        "last_seen_at",
        "idle_expires_at",
        "absolute_expires_at",
        "revoked_at",
    ]
    assert not {"token", "csrf_token", "password"}.intersection(auth_sessions.c.keys())
    checks = {
        constraint.name
        for constraint in auth_sessions.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_auth_sessions_csrf_hash",
        "ck_auth_sessions_expiry",
        "ck_auth_sessions_last_seen",
        "ck_auth_sessions_revoked_at",
        "ck_auth_sessions_token_hash",
    }
    foreign_keys = [
        constraint
        for constraint in auth_sessions.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    assert len(foreign_keys) == 1
    assert foreign_keys[0].name == "fk_auth_sessions_user"
    assert foreign_keys[0].ondelete == "CASCADE"
    assert {index.name for index in auth_sessions.indexes} == {
        "ix_auth_sessions_expired_cleanup",
        "ix_auth_sessions_user_active",
    }
    active = next(
        index for index in auth_sessions.indexes if index.name == "ix_auth_sessions_user_active"
    )
    assert active.dialect_options["postgresql"]["where"] is not None
