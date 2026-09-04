"""Scaled TP1/TP2/TP3 protection stages, breakeven tracking, and MFE/MAE diagnostics.

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

P = sa.Numeric(30, 12)
R = sa.Numeric(18, 6)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "live_positions",
        sa.Column("protection_stage", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("live_positions", sa.Column("tp1_filled_at", TS, nullable=True))
    op.add_column("live_positions", sa.Column("tp2_filled_at", TS, nullable=True))
    op.add_column("live_positions", sa.Column("tp3_filled_at", TS, nullable=True))
    op.add_column("live_positions", sa.Column("breakeven_moved_at", TS, nullable=True))
    op.add_column(
        "live_positions",
        sa.Column("max_favorable_excursion", P, nullable=False, server_default="0"),
    )
    op.add_column(
        "live_positions",
        sa.Column("max_adverse_excursion", P, nullable=False, server_default="0"),
    )
    op.add_column(
        "live_positions", sa.Column("mfe_r", R, nullable=False, server_default="0")
    )
    op.add_column(
        "live_positions", sa.Column("mae_r", R, nullable=False, server_default="0")
    )
    op.add_column(
        "live_positions", sa.Column("volatility_regime", sa.String(24), nullable=True)
    )

    op.add_column(
        "protective_orders",
        sa.Column("stage", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "protective_orders",
        sa.Column("role", sa.String(16), nullable=False, server_default="bracket"),
    )

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("protective_orders") as batch_op:
            batch_op.alter_column(
                "take_profit_client_order_id", existing_type=sa.String(64), nullable=True
            )
            batch_op.alter_column(
                "take_profit_price", existing_type=P, nullable=True
            )
    else:
        op.alter_column(
            "protective_orders",
            "take_profit_client_order_id",
            existing_type=sa.String(64),
            nullable=True,
        )
        op.alter_column(
            "protective_orders", "take_profit_price", existing_type=P, nullable=True
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("protective_orders") as batch_op:
            batch_op.alter_column(
                "take_profit_price", existing_type=P, nullable=False
            )
            batch_op.alter_column(
                "take_profit_client_order_id",
                existing_type=sa.String(64),
                nullable=False,
            )
    else:
        op.alter_column(
            "protective_orders", "take_profit_price", existing_type=P, nullable=False
        )
        op.alter_column(
            "protective_orders",
            "take_profit_client_order_id",
            existing_type=sa.String(64),
            nullable=False,
        )

    op.drop_column("protective_orders", "role")
    op.drop_column("protective_orders", "stage")

    op.drop_column("live_positions", "volatility_regime")
    op.drop_column("live_positions", "mae_r")
    op.drop_column("live_positions", "mfe_r")
    op.drop_column("live_positions", "max_adverse_excursion")
    op.drop_column("live_positions", "max_favorable_excursion")
    op.drop_column("live_positions", "breakeven_moved_at")
    op.drop_column("live_positions", "tp3_filled_at")
    op.drop_column("live_positions", "tp2_filled_at")
    op.drop_column("live_positions", "tp1_filled_at")
    op.drop_column("live_positions", "protection_stage")
