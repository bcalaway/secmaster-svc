"""Treasury STRIPS: strip, stripped_amount

mkt-data's docs/phase-3.md, step 3c: principal and interest STRIPS as
instruments, and the MSPD's monthly stripped amounts per security.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "strip",
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), primary_key=True),
        sa.Column("cusip", sa.String(length=9), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("tips", sa.Boolean(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("underlying_cusip", sa.String(length=9), nullable=True),
        sa.Column("underlying_sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=True),
        sa.Column("provenance", JSON_DOC, nullable=False),
        sa.Column("checks", JSON_DOC, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "stripped_amount",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("source_key", sa.String(length=40), nullable=False),
        sa.Column("strip_cusip", sa.String(length=9), nullable=False),
        sa.Column("underlying_cusip", sa.String(length=9), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=False),
        sa.Column("outstanding", sa.Numeric(), nullable=True),
        sa.Column("unstripped", sa.Numeric(), nullable=True),
        sa.Column("stripped", sa.Numeric(), nullable=True),
        sa.Column("reconstituted", sa.Numeric(), nullable=True),
        sa.Column("fields", JSON_DOC, nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_stripped_amount_key", "stripped_amount", ["source_key"], unique=True)
    op.create_index("ix_stripped_amount_underlying", "stripped_amount", ["underlying_cusip", "record_date"])
    op.create_index("ix_stripped_amount_period", "stripped_amount", ["period"])


def downgrade() -> None:
    for ix in ("ix_stripped_amount_period", "ix_stripped_amount_underlying", "uq_stripped_amount_key"):
        op.drop_index(ix, table_name="stripped_amount")
    op.drop_table("stripped_amount")
    op.drop_table("strip")
