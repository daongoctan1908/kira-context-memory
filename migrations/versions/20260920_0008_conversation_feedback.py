"""Add persisted assistant-answer feedback.

Revision ID: 20260920_0008
Revises: 20260919_0007
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0008"
down_revision: str | None = "20260919_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist one optional rating for each owned assistant turn."""
    op.create_table(
        "conversation_feedback",
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column("rating", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "length(turn_id) > 0",
            name="ck_conversation_feedback_turn_nonempty",
        ),
        sa.CheckConstraint(
            "rating IN ('up', 'down')",
            name="ck_conversation_feedback_rating",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            name="fk_conversation_feedback_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
        sa.UniqueConstraint(
            "conversation_id",
            "turn_id",
            name="uq_conversation_feedback_turn",
        ),
    )


def downgrade() -> None:
    """Remove answer feedback without touching conversation history."""
    op.drop_table("conversation_feedback")
