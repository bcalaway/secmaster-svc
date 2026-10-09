"""futures_figi_lookup: OpenFIGI's answers for listed futures contracts

mkt-data's docs/phase-4.md, step 2b.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "futures_figi_lookup",
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), primary_key=True),
        sa.Column("product_sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("root", sa.String(length=8), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("via", sa.String(length=10), nullable=True),
        sa.Column("figi", sa.String(length=12), nullable=True),
        sa.Column("composite_figi", sa.String(length=12), nullable=True),
        sa.Column("ticker", sa.String(length=60), nullable=True),
        sa.Column("bloomberg_root", sa.String(length=8), nullable=True),
        sa.Column("detail", JSON_DOC, nullable=True),
        sa.Column("looked_up_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("futures_figi_lookup")
