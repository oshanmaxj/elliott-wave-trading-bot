"""liquidity sweep and paper trade setup lifecycle

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa

from app.models import LiquiditySweep, TradeSetup

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("liquidity_pools")
    }
    if "status" not in columns:
        op.add_column(
            "liquidity_pools",
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        )
        op.create_index("ix_liquidity_pools_status", "liquidity_pools", ["status"])
    LiquiditySweep.__table__.create(bind=bind, checkfirst=True)
    TradeSetup.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    TradeSetup.__table__.drop(bind=op.get_bind(), checkfirst=True)
    LiquiditySweep.__table__.drop(bind=op.get_bind(), checkfirst=True)
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("liquidity_pools")
    }
    if "status" in columns:
        op.drop_column("liquidity_pools", "status")
