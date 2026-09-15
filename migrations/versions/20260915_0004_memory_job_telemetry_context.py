"""Add the bounded application-managed telemetry carrier to memory jobs.

Revision ID: 20260915_0004
Revises: 20260908_0003
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260915_0004"
down_revision: str | None = "20260908_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable JSONB carrier so old rows and bridge builds remain valid."""
    op.add_column(
        "memory_jobs",
        sa.Column("telemetry_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Remove only the additive telemetry carrier."""
    op.drop_column("memory_jobs", "telemetry_context")
