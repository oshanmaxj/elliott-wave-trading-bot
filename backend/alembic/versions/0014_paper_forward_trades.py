"""production-market paper forward trades

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None
P = sa.Numeric(30, 12)


def upgrade() -> None:
    op.create_table(
        "paper_forward_trades",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("setup_id", sa.Integer, nullable=False), sa.Column("symbol_id", sa.Integer, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False), sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False), sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("confidence_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("simulated_entry", P, nullable=False), sa.Column("entry_min", P, nullable=False), sa.Column("entry_max", P, nullable=False),
        sa.Column("stop_loss", P, nullable=False), sa.Column("active_stop", P, nullable=False),
        sa.Column("take_profit_1", P), sa.Column("take_profit_2", P), sa.Column("take_profit_3", P),
        sa.Column("next_target", sa.Integer, nullable=False, server_default="1"),
        sa.Column("initial_quantity", P, nullable=False), sa.Column("remaining_quantity", P, nullable=False),
        sa.Column("risk_amount", P, nullable=False), sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)), sa.Column("exit_price", P), sa.Column("exit_reason", sa.String(32)),
        sa.Column("realized_r", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("realized_pnl", P, nullable=False, server_default="0"), sa.Column("fees", P, nullable=False, server_default="0"),
        sa.Column("fee_rate_pct", sa.Numeric(8, 4), nullable=False, server_default="0.1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="waiting_entry"),
        sa.Column("max_favorable_excursion", P, nullable=False, server_default="0"),
        sa.Column("max_adverse_excursion", P, nullable=False, server_default="0"),
        sa.Column("mfe_r", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("mae_r", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("holding_bars", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_ambiguous", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("market_data_source", sa.String(64), nullable=False, server_default="binance_production_spot_db"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["setup_id"], ["trade_setups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("setup_id", name="uq_paper_forward_setup"),
    )
    for column in ("setup_id", "symbol_id", "symbol", "strategy", "direction", "timeframe", "confidence_score", "opened_at", "closed_at", "status", "is_ambiguous"):
        op.create_index(f"ix_paper_forward_trades_{column}", "paper_forward_trades", [column])


def downgrade() -> None:
    op.drop_table("paper_forward_trades")
