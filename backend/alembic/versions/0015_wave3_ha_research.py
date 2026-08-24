"""Wave-3 Heikin Ashi research audit records.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None
P = sa.Numeric(30, 12)


def upgrade() -> None:
    op.create_table(
        "wave3_ha_research_signals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("backtest_run_id", sa.Integer), sa.Column("symbol_id", sa.Integer, nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False, server_default="elliott_wave3_heikin_ashi_reversal"),
        sa.Column("variant", sa.String(1), nullable=False), sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("event_fingerprint", sa.String(128), nullable=False),
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("elliott_count_id", sa.Integer, nullable=False), sa.Column("wave_point_ids_json", sa.JSON, nullable=False),
        sa.Column("artifact_ids_json", sa.JSON, nullable=False), sa.Column("score", sa.Numeric(8, 4), nullable=False),
        sa.Column("score_components_json", sa.JSON, nullable=False), sa.Column("audit_json", sa.JSON, nullable=False),
        sa.Column("reversal_candle_id", sa.Integer, nullable=False), sa.Column("confirmation_candle_id", sa.Integer, nullable=False),
        sa.Column("exit_ha_candle_id", sa.Integer), sa.Column("real_entry", P, nullable=False),
        sa.Column("real_stop", P, nullable=False), sa.Column("real_exit", P), sa.Column("exit_reason", sa.String(40)),
        sa.Column("realized_r", sa.Numeric(18, 6)), sa.Column("mfe_r", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("mae_r", sa.Numeric(18, 6), nullable=False, server_default="0"), sa.Column("volatility_regime", sa.String(24)),
        sa.Column("holding_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("market_data_source", sa.String(64), nullable=False, server_default="binance_production_spot_db"),
        sa.Column("live_auto_execution_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["backtest_run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["elliott_count_id"], ["elliott_wave_counts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reversal_candle_id"], ["candles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmation_candle_id"], ["candles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["exit_ha_candle_id"], ["candles.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("event_fingerprint", "variant", name="uq_wave3_ha_event_variant"),
    )
    for col in ("backtest_run_id", "symbol_id", "strategy", "variant", "direction", "status", "event_fingerprint", "decision_time", "closed_at", "elliott_count_id", "score", "volatility_regime"):
        op.create_index(f"ix_wave3_ha_research_signals_{col}", "wave3_ha_research_signals", [col])


def downgrade() -> None:
    op.drop_table("wave3_ha_research_signals")
