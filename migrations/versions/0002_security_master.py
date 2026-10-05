"""security master: instrument, instrument_name, identifier, instrument_note, seed_run

Replaces the template's example `items` table (mkt-data's docs/phase-2.md,
Part B step 4).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool = False, default: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=sa.func.now() if default else None)


def upgrade() -> None:
    op.drop_table("items")
    op.create_table(
        "instrument",
        sa.Column("sec_id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("curve", sa.String(length=20), nullable=True),
        sa.Column("tenor", sa.String(length=10), nullable=True),
        sa.Column("calendar", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="active"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
    )
    op.create_table(
        "instrument_name",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        _ts("created_at", default=True),
        _ts("removed_at", nullable=True),
    )
    op.create_index("uq_instrument_name_name", "instrument_name", ["name"], unique=True)
    op.create_index(
        "uq_instrument_name_short", "instrument_name", ["sec_id"], unique=True,
        postgresql_where=sa.text("kind = 'short' AND removed_at IS NULL"),
        sqlite_where=sa.text("kind = 'short' AND removed_at IS NULL"),
    )
    op.create_table(
        "identifier",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("value", sa.String(length=60), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        _ts("created_at", default=True),
        _ts("removed_at", nullable=True),
    )
    op.create_index(
        "uq_identifier_current", "identifier", ["scheme", "value", "valid_from"], unique=True,
        # An open valid_from (NULL) must collide too (Postgres 15+; the hub runs 16).
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("removed_at IS NULL"), sqlite_where=sa.text("removed_at IS NULL"),
    )
    op.create_index("ix_identifier_sec_id", "identifier", ["sec_id"])
    op.create_table(
        "instrument_note",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sec_id", sa.Integer(), sa.ForeignKey("instrument.sec_id"), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        _ts("created_at", default=True),
        _ts("removed_at", nullable=True),
    )
    op.create_index(
        "uq_instrument_note_key", "instrument_note", ["sec_id", "key"], unique=True,
        postgresql_where=sa.text("removed_at IS NULL"), sqlite_where=sa.text("removed_at IS NULL"),
    )
    op.create_table(
        "seed_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("seed", sa.String(length=40), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        _ts("started_at"),
        _ts("finished_at", nullable=True),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("seed_run")
    op.drop_index("uq_instrument_note_key", table_name="instrument_note")
    op.drop_table("instrument_note")
    op.drop_index("ix_identifier_sec_id", table_name="identifier")
    op.drop_index("uq_identifier_current", table_name="identifier")
    op.drop_table("identifier")
    op.drop_index("uq_instrument_name_short", table_name="instrument_name")
    op.drop_index("uq_instrument_name_name", table_name="instrument_name")
    op.drop_table("instrument_name")
    op.drop_table("instrument")
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
