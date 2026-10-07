"""cpi_month and reference_cpi: TIPS reference CPI from BLS's CPI-U

mkt-data's docs/phase-3.md, step 3e.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cpi_month",
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("observation_id", sa.Integer(), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "reference_cpi",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("reference_cpi")
    op.drop_table("cpi_month")
