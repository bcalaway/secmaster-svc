"""swap_terms: the OIS par swaps' conventions (mkt-data docs/phase-4.md, "Swap curves")

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT = {"postgresql_where": sa.text("superseded_at IS NULL"), "sqlite_where": sa.text("superseded_at IS NULL")}


def upgrade() -> None:
    op.create_table(
        "swap_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("curve", sa.String(length=20), nullable=False),
        sa.Column("floating_index", sa.String(length=20), nullable=False),
        sa.Column("money_market_day_count", sa.String(length=16), nullable=False),
        sa.Column("fixed_day_count", sa.String(length=16), nullable=False),
        sa.Column("floating_day_count", sa.String(length=16), nullable=False),
        sa.Column("fixed_frequency", sa.String(length=10), nullable=False),
        sa.Column("floating_frequency", sa.String(length=10), nullable=False),
        sa.Column("coupon", sa.String(length=10), nullable=False),
        sa.Column("zero_coupon_through", sa.String(length=10), nullable=False),
        sa.Column("spot_lag_days", sa.SmallInteger(), nullable=False),
        sa.Column("spot_calendar", sa.String(length=20), nullable=True),
        sa.Column("adjust_calendar", sa.String(length=20), nullable=True),
        sa.Column("business_day_convention", sa.String(length=20), nullable=False),
        sa.Column("model_instrument_type", sa.String(length=4), nullable=False),
        sa.Column("snap_time", sa.String(length=5), nullable=False),
        sa.Column("publication_time", sa.String(length=5), nullable=False),
        sa.Column("publication_deadline", sa.String(length=5), nullable=False),
        sa.Column("timezone", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.String(length=10), nullable=False),
        sa.Column("cite", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_swap_terms_current", "swap_terms", ["sec_id"], unique=True, **CURRENT)


def downgrade() -> None:
    op.drop_index("uq_swap_terms_current", table_name="swap_terms")
    op.drop_table("swap_terms")
