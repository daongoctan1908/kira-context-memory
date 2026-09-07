"""Scope conversations by trusted end-user identity.

Revision ID: 20260906_0002
Revises: 20260903_0001
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0002"
down_revision: str | None = "20260903_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Preserve existing rows under isolated legacy owners, then require user scope."""
    op.add_column("conversations", sa.Column("user_id", sa.Text(), nullable=True))
    op.execute(
        "UPDATE conversations "
        "SET user_id = 'legacy:' || conversation_id::text "
        "WHERE user_id IS NULL"
    )
    op.alter_column("conversations", "user_id", nullable=False)
    op.drop_constraint("conversations_session_id_key", "conversations", type_="unique")
    op.create_unique_constraint(
        "uq_conversations_user_session",
        "conversations",
        ["user_id", "session_id"],
    )


def downgrade() -> None:
    """Restore global session uniqueness when the current data permits it."""
    op.drop_constraint("uq_conversations_user_session", "conversations", type_="unique")
    op.create_unique_constraint("conversations_session_id_key", "conversations", ["session_id"])
    op.drop_column("conversations", "user_id")
