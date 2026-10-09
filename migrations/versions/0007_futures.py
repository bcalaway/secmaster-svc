"""futures_product, futures_spec, futures_contract, futures_run: CME rates and FX futures

mkt-data's docs/phase-4.md, step 2a.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
CURRENT = {"postgresql_where": sa.text("superseded_at IS NULL"), "sqlite_where": sa.text("superseded_at IS NULL")}


def upgrade() -> None:
    op.create_table(
        "futures_product",
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), primary_key=True),
        sa.Column("root", sa.String(length=8), nullable=False),
        sa.Column("cme_code", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("info", JSON_DOC, nullable=False),
        sa.Column("seed_sha256", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_futures_product_cme_code", "futures_product", ["cme_code"], unique=True)

    op.create_table(
        "futures_spec",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fields", JSON_DOC, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_futures_spec_sec_id", "futures_spec", ["sec_id"])

    op.create_table(
        "futures_contract",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("product_sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("contract_month", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("first_trade_date", sa.Date(), nullable=True),
        sa.Column("last_trade_date", sa.Date(), nullable=False),
        sa.Column("first_intention_date", sa.Date(), nullable=True),
        sa.Column("first_notice_date", sa.Date(), nullable=True),
        sa.Column("first_delivery_date", sa.Date(), nullable=True),
        sa.Column("last_delivery_date", sa.Date(), nullable=True),
        sa.Column("reference_start", sa.Date(), nullable=True),
        sa.Column("reference_end", sa.Date(), nullable=True),
        sa.Column("final_settlement_date", sa.Date(), nullable=True),
        sa.Column("settlement_date", sa.Date(), nullable=True),
        sa.Column("rules", JSON_DOC, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_futures_contract_current", "futures_contract", ["sec_id"], unique=True, **CURRENT)
    op.create_index("uq_futures_contract_month", "futures_contract", ["product_sec_id", "contract_month"],
                    unique=True, **CURRENT)
    op.create_index("ix_futures_contract_last_trade", "futures_contract", ["last_trade_date"])

    op.create_table(
        "futures_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("seed_sha256", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.CheckConstraint("outcome IN ('ok', 'error')", name="ck_futures_run_outcome"),
    )


def downgrade() -> None:
    op.drop_table("futures_run")
    op.drop_index("ix_futures_contract_last_trade", table_name="futures_contract")
    op.drop_index("uq_futures_contract_month", table_name="futures_contract")
    op.drop_index("uq_futures_contract_current", table_name="futures_contract")
    op.drop_table("futures_contract")
    op.drop_index("ix_futures_spec_sec_id", table_name="futures_spec")
    op.drop_table("futures_spec")
    op.drop_index("uq_futures_product_cme_code", table_name="futures_product")
    op.drop_table("futures_product")
