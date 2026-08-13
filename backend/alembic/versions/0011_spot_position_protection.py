"""Persist exchange-backed Spot position protection.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

P = sa.Numeric(30, 12)
TS = sa.DateTime(timezone=True)


def upgrade():
    op.add_column(
        "live_positions",
        sa.Column(
            "protection_status",
            sa.String(length=32),
            nullable=False,
            server_default="unprotected",
        ),
    )
    op.create_index(
        "ix_live_positions_protection_status",
        "live_positions",
        ["protection_status"],
    )
    op.create_table(
        "protective_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("live_position_id", sa.Integer(), sa.ForeignKey("live_positions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("symbol_id", sa.Integer(), sa.ForeignKey("symbols.id"), nullable=False),
        sa.Column("order_list_id", sa.String(64)),
        sa.Column("list_client_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("stop_client_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("take_profit_client_order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("stop_exchange_order_id", sa.String(64)),
        sa.Column("take_profit_exchange_order_id", sa.String(64)),
        sa.Column("quantity", P, nullable=False),
        sa.Column("stop_price", P, nullable=False),
        sa.Column("take_profit_price", P, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("raw_status_json", sa.JSON(), nullable=False),
        sa.Column("rejection_reason", sa.String(500)),
        sa.Column("submitted_at", TS),
        sa.Column("acknowledged_at", TS),
        sa.Column("closed_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    for column in (
        "live_position_id",
        "environment",
        "symbol_id",
        "order_list_id",
        "list_client_order_id",
        "stop_client_order_id",
        "take_profit_client_order_id",
        "status",
    ):
        op.create_index(
            f"ix_protective_orders_{column}", "protective_orders", [column]
        )


def downgrade():
    op.drop_table("protective_orders")
    op.drop_index("ix_live_positions_protection_status", table_name="live_positions")
    op.drop_column("live_positions", "protection_status")
