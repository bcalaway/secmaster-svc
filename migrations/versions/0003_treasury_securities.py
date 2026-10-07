"""Treasury securities: security_terms, auction, source_period, untyped_record, load_run

mkt-data's docs/phase-3.md, step 3: Treasury bills, notes, bonds, TIPS and
FRNs as instruments, built from mkt-data's near-raw TreasuryDirect records.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "security_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("cusip", sa.String(length=9), nullable=False),
        sa.Column("security_type", sa.String(length=10), nullable=False),
        sa.Column("cmb", sa.Boolean(), nullable=False),
        sa.Column("term", sa.String(length=20), nullable=True),
        sa.Column("original_term", sa.String(length=30), nullable=True),
        sa.Column("announcement_date", sa.Date(), nullable=True),
        sa.Column("auction_date", sa.Date(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("dated_date", sa.Date(), nullable=True),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column("coupon_rate", sa.Numeric(), nullable=True),
        sa.Column("coupon_frequency", sa.SmallInteger(), nullable=False),
        sa.Column("day_count", sa.String(length=16), nullable=False),
        sa.Column("first_coupon_date", sa.Date(), nullable=True),
        sa.Column("first_period_type", sa.String(length=10), nullable=True),
        sa.Column("penultimate_coupon_date", sa.Date(), nullable=True),
        sa.Column("end_of_month", sa.Boolean(), nullable=True),
        sa.Column("redemption", sa.Numeric(), nullable=False),
        sa.Column("settlement_days", sa.SmallInteger(), nullable=False),
        sa.Column("calendar", sa.String(length=20), nullable=False),
        sa.Column("callable", sa.Boolean(), nullable=True),
        sa.Column("call_date", sa.Date(), nullable=True),
        sa.Column("called_date", sa.Date(), nullable=True),
        sa.Column("strippable", sa.Boolean(), nullable=True),
        sa.Column("corpus_cusip", sa.String(length=9), nullable=True),
        sa.Column("tips_base_cpi", sa.Numeric(), nullable=True),
        sa.Column("cpi_base_period", sa.String(length=20), nullable=True),
        sa.Column("frn_spread", sa.Numeric(), nullable=True),
        sa.Column("frn_index", sa.String(length=40), nullable=True),
        sa.Column("provenance", JSON_DOC, nullable=False),
        sa.Column("checks", JSON_DOC, nullable=False),
        _ts("recorded_at"),
        _ts("superseded_at", nullable=True),
    )
    op.create_index("uq_security_terms_current", "security_terms", ["sec_id"], unique=True,
                    postgresql_where=sa.text("superseded_at IS NULL"), sqlite_where=sa.text("superseded_at IS NULL"))
    op.create_index("ix_security_terms_maturity", "security_terms", ["maturity_date"])

    op.create_table(
        "auction",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("source_key", sa.String(length=40), nullable=False),
        sa.Column("cusip", sa.String(length=9), nullable=False),
        sa.Column("security_type", sa.String(length=10), nullable=False),
        sa.Column("reopening", sa.Boolean(), nullable=False),
        sa.Column("term", sa.String(length=20), nullable=True),
        sa.Column("security_term", sa.String(length=30), nullable=True),
        sa.Column("announcement_date", sa.Date(), nullable=True),
        sa.Column("auction_date", sa.Date(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        *[sa.Column(name, sa.Numeric(), nullable=True) for name in (
            "offering_amount", "total_tendered", "total_accepted", "bid_to_cover", "high_yield",
            "high_discount_rate", "high_investment_rate", "high_discount_margin", "high_price", "price_per_100",
            "accrued_interest_per_1000", "adjusted_accrued_interest_per_1000", "index_ratio_on_issue_date",
            "ref_cpi_on_issue_date", "frn_index_rate")],
        sa.Column("frn_index_determination_date", sa.Date(), nullable=True),
        sa.Column("currently_outstanding", sa.Numeric(), nullable=True),
        sa.Column("fields", JSON_DOC, nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        _ts("loaded_at"),
        _ts("updated_at"),
        _ts("removed_at", nullable=True),
    )
    op.create_index("uq_auction_key", "auction", ["source", "source_key"], unique=True)
    op.create_index("ix_auction_sec_id", "auction", ["sec_id"])
    op.create_index("ix_auction_period", "auction", ["source", "period"])
    op.create_index("ix_auction_term_date", "auction", ["security_type", "term", "auction_date"])

    op.create_table(
        "source_period",
        sa.Column("source", sa.String(length=20), primary_key=True),
        sa.Column("period", sa.String(length=10), primary_key=True),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column("records", sa.Integer(), nullable=False),
        _ts("loaded_at"),
    )
    op.create_table(
        "untyped_record",
        sa.Column("source", sa.String(length=20), primary_key=True),
        sa.Column("source_key", sa.String(length=80), primary_key=True),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False),
        _ts("first_seen"),
        _ts("last_seen"),
    )
    op.create_table(
        "load_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.CheckConstraint("outcome IN ('ok', 'error')", name="ck_load_run_outcome"),
    )


def downgrade() -> None:
    op.drop_table("load_run")
    op.drop_table("untyped_record")
    op.drop_table("source_period")
    for ix in ("ix_auction_term_date", "ix_auction_period", "ix_auction_sec_id", "uq_auction_key"):
        op.drop_index(ix, table_name="auction")
    op.drop_table("auction")
    op.drop_index("ix_security_terms_maturity", table_name="security_terms")
    op.drop_index("uq_security_terms_current", table_name="security_terms")
    op.drop_table("security_terms")
