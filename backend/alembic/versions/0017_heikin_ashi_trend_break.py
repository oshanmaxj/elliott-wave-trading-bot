"""Heikin Ashi trend-break research audit records.

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None
P = sa.Numeric(30, 12)


def upgrade() -> None:
    op.create_table(
        "heikin_ashi_trend_break_signals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol_id", sa.Integer, nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False, server_default="heikin_ashi_trend_break"),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("event_fingerprint", sa.String(128), nullable=False, unique=True),
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("bearish_ref_candle_id", sa.Integer, nullable=False),
        sa.Column("bullish_ref_candle_id", sa.Integer, nullable=False),
        sa.Column("entry_ha_candle_id", sa.Integer, nullable=False),
        sa.Column("exit_ha_candle_id", sa.Integer),
        sa.Column("real_entry", P, nullable=False),
        sa.Column("real_stop", P, nullable=False),
        sa.Column("real_exit", P),
        sa.Column("exit_reason", sa.String(40)),
        sa.Column("realized_r", sa.Numeric(18, 6)),
        sa.Column("mfe_r", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("mae_r", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("holding_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("volatility_regime", sa.String(24)),
        sa.Column("market_data_source", sa.String(64), nullable=False, server_default="binance_production_spot_db"),
        sa.Column("live_auto_execution_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bearish_ref_candle_id"], ["candles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["bullish_ref_candle_id"], ["candles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["entry_ha_candle_id"], ["candles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["exit_ha_candle_id"], ["candles.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("event_fingerprint", name="uq_ha_trend_break_event_fingerprint"),
    )
    for col in ("symbol_id", "strategy", "direction", "status", "event_fingerprint", "decision_time", "closed_at", "volatility_regime"):
        op.create_index(f"ix_heikin_ashi_trend_break_signals_{col}", "heikin_ashi_trend_break_signals", [col])


def downgrade() -> None:
    op.drop_table("heikin_ashi_trend_break_signals")
