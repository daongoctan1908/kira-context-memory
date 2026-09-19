"""Create application-managed users and opaque authentication sessions.

Revision ID: 20260919_0005
Revises: 20260915_0004
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0005"
down_revision: str | None = "20260915_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add credential records and revocable, server-side opaque sessions."""
    op.create_table(
        "auth_users",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "failed_login_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "username = lower(username) AND username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'",
            name="ck_auth_users_username_normalized",
        ),
        sa.CheckConstraint(
            "length(password_hash) > 0", name="ck_auth_users_password_hash_nonempty"
        ),
        sa.CheckConstraint("failed_login_count >= 0", name="ck_auth_users_failed_login_count"),
        sa.CheckConstraint(
            "locked_until IS NULL OR locked_until >= created_at",
            name="ck_auth_users_locked_until",
        ),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("username", name="uq_auth_users_username"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("csrf_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="ck_auth_sessions_token_hash"),
        sa.CheckConstraint("octet_length(csrf_token_hash) = 32", name="ck_auth_sessions_csrf_hash"),
        sa.CheckConstraint("last_seen_at >= created_at", name="ck_auth_sessions_last_seen"),
        sa.CheckConstraint(
            "idle_expires_at > created_at AND idle_expires_at <= absolute_expires_at",
            name="ck_auth_sessions_expiry",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_auth_sessions_revoked_at",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth_users.user_id"],
            name="fk_auth_sessions_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index(
        "ix_auth_sessions_user_active",
        "auth_sessions",
        ["user_id", "absolute_expires_at"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_auth_sessions_expired_cleanup",
        "auth_sessions",
        ["absolute_expires_at", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove auth state without touching conversations or memory jobs."""
    op.drop_index("ix_auth_sessions_expired_cleanup", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_active", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("auth_users")
