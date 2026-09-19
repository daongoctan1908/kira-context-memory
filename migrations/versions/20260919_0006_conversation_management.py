"""Add owned conversation management metadata and activity ordering.

Revision ID: 20260919_0006
Revises: 20260919_0005
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0006"
down_revision: str | None = "20260919_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add title, lifecycle state and a stable activity cursor."""
    op.add_column("conversations", sa.Column("title", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_conversations_title",
        "conversations",
        "title IS NULL OR (title = btrim(title) AND length(title) BETWEEN 1 AND 200)",
    )
    op.create_check_constraint(
        "ck_conversations_status",
        "conversations",
        "status IN ('active', 'deletion_pending')",
    )
    op.execute(
        "UPDATE conversations AS c "
        "SET last_message_at = latest.last_message_at "
        "FROM ("
        "SELECT conversation_id, MAX(message_timestamp) AS last_message_at "
        "FROM conversation_messages GROUP BY conversation_id"
        ") AS latest "
        "WHERE latest.conversation_id = c.conversation_id"
    )
    op.create_index(
        "ix_conversations_user_activity",
        "conversations",
        [
            "user_id",
            sa.text("COALESCE(last_message_at, created_at) DESC"),
            sa.text("conversation_id DESC"),
        ],
        unique=False,
    )


def downgrade() -> None:
    """Remove product conversation metadata without touching message history."""
    op.drop_index("ix_conversations_user_activity", table_name="conversations")
    op.drop_constraint("ck_conversations_status", "conversations", type_="check")
    op.drop_constraint("ck_conversations_title", "conversations", type_="check")
    op.drop_column("conversations", "last_message_at")
    op.drop_column("conversations", "status")
    op.drop_column("conversations", "title")
