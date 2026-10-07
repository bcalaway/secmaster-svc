"""figi_lookup: what OpenFIGI said about each CUSIP

mkt-data's docs/phase-3.md, step 3d.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "figi_lookup",
        sa.Column("cusip", sa.String(length=9), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("figi", sa.String(length=12), nullable=True),
        sa.Column("composite_figi", sa.String(length=12), nullable=True),
        sa.Column("ticker", sa.String(length=60), nullable=True),
        sa.Column("detail", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("looked_up_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("figi_lookup")
