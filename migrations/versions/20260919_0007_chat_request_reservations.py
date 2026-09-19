"""Add fenced idempotency reservations for durable chat requests.

Revision ID: 20260919_0007
Revises: 20260919_0006
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0007"
down_revision: str | None = "20260919_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one fenced request record per client message and conversation."""
    op.create_table(
        "chat_requests",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("client_message_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "attempt_count",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32",
            name="ck_chat_requests_content_hash",
        ),
        sa.CheckConstraint(
            "length(turn_id) > 0",
            name="ck_chat_requests_turn_id_nonempty",
        ),
        sa.CheckConstraint("attempt_count >= 1", name="ck_chat_requests_attempt_count"),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed', 'cancelled')",
            name="ck_chat_requests_status",
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('failed', 'cancelled') AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NOT NULL)",
            name="ck_chat_requests_lifecycle",
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > updated_at",
            name="ck_chat_requests_lease_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name="fk_chat_requests_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("request_id"),
        sa.UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_chat_requests_conversation_client_message",
        ),
        sa.UniqueConstraint("turn_id", name="uq_chat_requests_turn_id"),
    )
    op.create_index(
        "uq_chat_requests_conversation_processing",
        "chat_requests",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_chat_requests_processing_lease",
        "chat_requests",
        ["lease_expires_at", "request_id"],
        unique=False,
        postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    """Remove chat idempotency state without touching completed conversations."""
    op.drop_index("ix_chat_requests_processing_lease", table_name="chat_requests")
    op.drop_index("uq_chat_requests_conversation_processing", table_name="chat_requests")
    op.drop_table("chat_requests")
