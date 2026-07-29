"""paper-only backtesting and execution

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None
P = sa.Numeric(30, 12)


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Integer, primary_key=True), sa.Column("symbol_id", sa.Integer, nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False), sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False), sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("starting_balance", P, nullable=False), sa.Column("risk_per_trade_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("total_setups", sa.Integer, nullable=False),
        sa.Column("trades_taken", sa.Integer, nullable=False), sa.Column("wins", sa.Integer, nullable=False),
        sa.Column("losses", sa.Integer, nullable=False), sa.Column("break_even", sa.Integer, nullable=False),
        sa.Column("gross_profit", P, nullable=False), sa.Column("gross_loss", P, nullable=False),
        sa.Column("net_profit", P, nullable=False), sa.Column("profit_factor", sa.Numeric(18, 6)),
        sa.Column("win_rate", sa.Numeric(8, 4), nullable=False), sa.Column("max_drawdown_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("expectancy", sa.Numeric(18, 6), nullable=False), sa.Column("average_rr", sa.Numeric(18, 6), nullable=False),
        sa.Column("sharpe_like_ratio", sa.Numeric(18, 6)), sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("settings_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_backtest_runs_symbol_id", "backtest_runs", ["symbol_id"])
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"])
    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.Integer, primary_key=True), sa.Column("backtest_run_id", sa.Integer, nullable=False),
        sa.Column("trade_setup_id", sa.Integer), sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False), sa.Column("entry_price", P, nullable=False),
        sa.Column("stop_loss", P, nullable=False), sa.Column("take_profit_1", P), sa.Column("take_profit_2", P), sa.Column("take_profit_3", P),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False), sa.Column("exit_price", P, nullable=False),
        sa.Column("exit_reason", sa.String(32), nullable=False), sa.Column("risk_amount", P, nullable=False),
        sa.Column("quantity", P, nullable=False), sa.Column("fees", P, nullable=False), sa.Column("slippage", P, nullable=False),
        sa.Column("realized_pnl", P, nullable=False), sa.Column("realized_r", sa.Numeric(18, 6), nullable=False),
        sa.Column("mae", P, nullable=False), sa.Column("mfe", P, nullable=False), sa.Column("holding_bars", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["backtest_run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trade_setup_id"], ["trade_setups.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_backtest_trades_backtest_run_id", "backtest_trades", ["backtest_run_id"])
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("starting_balance", P, nullable=False), sa.Column("balance", P, nullable=False), sa.Column("equity", P, nullable=False),
        sa.Column("realized_pnl", P, nullable=False), sa.Column("unrealized_pnl", P, nullable=False), sa.Column("max_equity", P, nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(8, 4), nullable=False), sa.Column("risk_per_trade_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("max_daily_loss_pct", sa.Numeric(8, 4), nullable=False), sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "paper_positions",
        sa.Column("id", sa.Integer, primary_key=True), sa.Column("account_id", sa.Integer, nullable=False),
        sa.Column("trade_setup_id", sa.Integer, nullable=False), sa.Column("symbol_id", sa.Integer, nullable=False),
        sa.Column("direction", sa.String(16), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("entry_price", P, nullable=False), sa.Column("quantity", P, nullable=False), sa.Column("stop_loss", P, nullable=False),
        sa.Column("tp1", P), sa.Column("tp2", P), sa.Column("tp3", P), sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)), sa.Column("exit_price", P), sa.Column("exit_reason", sa.String(32)),
        sa.Column("realized_pnl", P, nullable=False), sa.Column("realized_r", sa.Numeric(18, 6), nullable=False),
        sa.Column("fees", P, nullable=False), sa.Column("slippage", P, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trade_setup_id"], ["trade_setups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
    )
    for column in ("account_id", "trade_setup_id", "symbol_id", "status"):
        op.create_index(f"ix_paper_positions_{column}", "paper_positions", [column])


def downgrade() -> None:
    op.drop_table("paper_positions")
    op.drop_table("paper_accounts")
    op.drop_table("backtest_trades")
    op.drop_table("backtest_runs")
