"""Create durable conversation tables.

Revision ID: 20260903_0001
Revises:
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create persistent conversations and their ordered messages."""
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column(
            "next_turn_sequence",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
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
            "next_turn_sequence >= 1",
            name="ck_conversations_next_turn_sequence",
        ),
        sa.PrimaryKeyConstraint("conversation_id"),
        sa.UniqueConstraint("session_id"),
    )
    op.create_table(
        "conversation_messages",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column("turn_sequence", sa.BigInteger(), nullable=False),
        sa.Column("message_index", sa.SmallInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(content) > 0", name="ck_messages_content_nonempty"),
        sa.CheckConstraint("message_index IN (0, 1)", name="ck_messages_message_index"),
        sa.CheckConstraint(
            "(message_index = 0 AND role = 'user') OR (message_index = 1 AND role = 'assistant')",
            name="ck_messages_role_position",
        ),
        sa.CheckConstraint("turn_sequence >= 1", name="ck_messages_turn_sequence"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint(
            "conversation_id",
            "turn_sequence",
            "message_index",
            name="uq_messages_conversation_sequence_position",
        ),
        sa.UniqueConstraint("turn_id", "message_index", name="uq_messages_turn_position"),
    )
    op.create_index(
        "ix_messages_conversation_recent",
        "conversation_messages",
        ["conversation_id", sa.text("turn_sequence DESC"), sa.text("message_index DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Remove durable conversation storage."""
    op.drop_index("ix_messages_conversation_recent", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
