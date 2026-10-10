"""Futures contracts: one instrument type, `future`

Contracts were typed by kind (fut_treasury, fut_stir, fut_fx); every contract is now an instrument of type
`future` (Bill, 2026-10-10), the kind staying on its product (futures_product.kind). The futures job types the
contracts it generates; this retypes the ones it no longer generates (expired before a product's history start,
withdrawn) too.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE instrument SET type = 'future' WHERE type IN ('fut_treasury', 'fut_stir', 'fut_fx')")


def downgrade() -> None:
    # Back to the kind's type, through the contract's product (treasury_cash was fut_treasury, fx_cash fut_fx).
    op.execute("""
        UPDATE instrument SET type = CASE (
            SELECT p.kind FROM futures_contract c JOIN futures_product p ON p.sec_id = c.product_sec_id
            WHERE c.sec_id = instrument.sec_id LIMIT 1)
          WHEN 'stir' THEN 'fut_stir' WHEN 'fx' THEN 'fut_fx' WHEN 'fx_cash' THEN 'fut_fx' ELSE 'fut_treasury' END
        WHERE type = 'future'
    """)
