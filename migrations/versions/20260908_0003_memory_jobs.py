"""Create the durable PostgreSQL memory job queue.

Revision ID: 20260908_0003
Revises: 20260906_0002
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_0003"
down_revision: str | None = "20260906_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create reference-only jobs with constrained lifecycle state and queue indexes."""
    op.create_table(
        "memory_jobs",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("boundary_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "schema_version",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "requeue_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.Text(), nullable=True),
        sa.Column("lifecycle_event_count", sa.Integer(), nullable=True),
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
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("schema_version = 1", name="ck_memory_jobs_schema_version"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND requeue_count >= 0 "
            "AND (lifecycle_event_count IS NULL OR lifecycle_event_count >= 0)",
            name="ck_memory_jobs_counts",
        ),
        sa.CheckConstraint(
            "status = 'pending' OR attempt_count >= 1",
            name="ck_memory_jobs_attempt_state",
        ),
        sa.CheckConstraint(
            "last_error_class IS NULL OR last_error_class ~ '^[A-Za-z_][A-Za-z0-9_]{0,127}$'",
            name="ck_memory_jobs_error_class",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'dead')",
            name="ck_memory_jobs_status",
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'processing' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_memory_jobs_lease_state",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND dead_at IS NULL "
            "AND lifecycle_event_count IS NULL) "
            "OR (status = 'processing' AND completed_at IS NULL AND dead_at IS NULL "
            "AND lifecycle_event_count IS NULL) "
            "OR (status = 'completed' AND completed_at IS NOT NULL AND dead_at IS NULL "
            "AND lifecycle_event_count IS NOT NULL) "
            "OR (status = 'dead' AND completed_at IS NULL AND dead_at IS NOT NULL "
            "AND lifecycle_event_count IS NULL AND last_error_class IS NOT NULL)",
            name="ck_memory_jobs_terminal_state",
        ),
        sa.CheckConstraint(
            "(completed_at IS NULL OR completed_at >= created_at) "
            "AND (dead_at IS NULL OR dead_at >= created_at)",
            name="ck_memory_jobs_terminal_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["boundary_message_id"],
            ["conversation_messages.message_id"],
            name="fk_memory_jobs_boundary_message",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "boundary_message_id",
            name="uq_memory_jobs_boundary_message",
        ),
    )
    op.create_index(
        "ix_memory_jobs_pending_due",
        "memory_jobs",
        ["next_attempt_at", "created_at", "event_id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_memory_jobs_processing_lease",
        "memory_jobs",
        ["lease_expires_at", "event_id"],
        unique=False,
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_memory_jobs_completed_cleanup",
        "memory_jobs",
        ["completed_at", "event_id"],
        unique=False,
        postgresql_where=sa.text("status = 'completed'"),
    )
    op.create_index(
        "ix_memory_jobs_dead_cleanup",
        "memory_jobs",
        ["dead_at", "event_id"],
        unique=False,
        postgresql_where=sa.text("status = 'dead'"),
    )


def downgrade() -> None:
    """Remove the memory job queue without changing persisted conversations."""
    op.drop_index("ix_memory_jobs_dead_cleanup", table_name="memory_jobs")
    op.drop_index("ix_memory_jobs_completed_cleanup", table_name="memory_jobs")
    op.drop_index("ix_memory_jobs_processing_lease", table_name="memory_jobs")
    op.drop_index("ix_memory_jobs_pending_due", table_name="memory_jobs")
    op.drop_table("memory_jobs")
