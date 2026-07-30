"""paper execution metadata

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None
P = sa.Numeric(30, 12)


def upgrade() -> None:
    op.add_column("paper_positions", sa.Column("initial_quantity", P, nullable=False, server_default="0"))
    op.add_column("paper_positions", sa.Column("risk_amount", P, nullable=False, server_default="0"))
    op.add_column("paper_positions", sa.Column("taker_fee_pct", sa.Numeric(8, 4), nullable=False, server_default="0.05"))
    op.add_column("paper_positions", sa.Column("slippage_bps", sa.Numeric(8, 4), nullable=False, server_default="2"))


def downgrade() -> None:
    op.drop_column("paper_positions", "slippage_bps")
    op.drop_column("paper_positions", "taker_fee_pct")
    op.drop_column("paper_positions", "risk_amount")
    op.drop_column("paper_positions", "initial_quantity")
