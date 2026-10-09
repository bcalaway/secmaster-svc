"""futures_deliverable: the Treasury futures' baskets and conversion factors (mkt-data docs/phase-4.md, step 3)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT = {"postgresql_where": sa.text("superseded_at IS NULL"), "sqlite_where": sa.text("superseded_at IS NULL")}


def upgrade() -> None:
    op.create_table(
        "futures_deliverable",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contract_sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("security_sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("conversion_factor", sa.Numeric(), nullable=False),
        sa.Column("remaining_months", sa.SmallInteger(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("rule", sa.String(length=200), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_futures_deliverable_current", "futures_deliverable", ["contract_sec_id", "security_sec_id"],
                    unique=True, **CURRENT)
    op.create_index("ix_futures_deliverable_security", "futures_deliverable", ["security_sec_id"])


def downgrade() -> None:
    op.drop_index("ix_futures_deliverable_security", table_name="futures_deliverable")
    op.drop_index("uq_futures_deliverable_current", table_name="futures_deliverable")
    op.drop_table("futures_deliverable")
