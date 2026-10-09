"""futures_product.kind: room for treasury_cash

String(10) was too short for step 2c's `treasury_cash` (13 characters); Postgres refused the insert
(2026-10-09). SQLite, which the tests use, doesn't enforce the length.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("futures_product") as t:
        t.alter_column("kind", type_=sa.String(length=20), existing_type=sa.String(length=10), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("futures_product") as t:
        t.alter_column("kind", type_=sa.String(length=10), existing_type=sa.String(length=20), existing_nullable=False)
