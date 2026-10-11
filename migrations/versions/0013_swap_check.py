"""swap_check: the latest check of each SPGMI swap curve's files against swap_terms (mkt-data docs/phase-4.md)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "swap_check",
        sa.Column("source", sa.String(length=40), primary_key=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("files", sa.Integer(), nullable=False),
        sa.Column("last_period", sa.String(length=10), nullable=True),
        sa.Column("detail", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("swap_check")
