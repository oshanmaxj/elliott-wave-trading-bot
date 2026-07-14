"""smart money concepts tables

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    price = sa.Numeric(30, 12)
    if "liquidity_pools" not in existing:
        op.create_table("liquidity_pools", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("symbol_id", sa.Integer(), sa.ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False), sa.Column("timeframe", sa.String(8), nullable=False), sa.Column("type", sa.String(16), nullable=False), sa.Column("price", price, nullable=False), sa.Column("strength", sa.Numeric(8, 4), nullable=False), sa.Column("first_swing_id", sa.Integer(), sa.ForeignKey("swing_points.id", ondelete="CASCADE"), nullable=False), sa.Column("second_swing_id", sa.Integer(), sa.ForeignKey("swing_points.id", ondelete="CASCADE"), nullable=False), sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False), sa.Column("swept_at", sa.DateTime(timezone=True)), sa.Column("metadata_json", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("type", "first_swing_id", "second_swing_id", name="uq_liquidity_swing_pair"))
        op.create_index("ix_liquidity_symbol", "liquidity_pools", ["symbol_id", "timeframe"])
    if "order_blocks" not in existing:
        op.create_table("order_blocks", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("symbol_id", sa.Integer(), sa.ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False), sa.Column("timeframe", sa.String(8), nullable=False), sa.Column("direction", sa.String(16), nullable=False), sa.Column("candle_id", sa.Integer(), sa.ForeignKey("candles.id", ondelete="CASCADE"), nullable=False), sa.Column("top_price", price, nullable=False), sa.Column("bottom_price", price, nullable=False), sa.Column("bos_event_id", sa.Integer(), sa.ForeignKey("market_structure_events.id", ondelete="CASCADE"), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("mitigation_percent", sa.Numeric(8, 4), nullable=False), sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False), sa.Column("first_touched_at", sa.DateTime(timezone=True)), sa.Column("fully_mitigated_at", sa.DateTime(timezone=True)), sa.Column("invalidated_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("bos_event_id", name="uq_order_block_bos"))
        op.create_index("ix_order_blocks_symbol", "order_blocks", ["symbol_id", "timeframe"])
    if "alerts" not in existing:
        op.create_table("alerts", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("type", sa.String(32), nullable=False), sa.Column("symbol_id", sa.Integer(), sa.ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False), sa.Column("timeframe", sa.String(8), nullable=False), sa.Column("message", sa.String(500), nullable=False), sa.Column("source_type", sa.String(32), nullable=False), sa.Column("source_id", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("type", "source_type", "source_id", name="uq_alert_source"))
        op.create_index("ix_alerts_symbol", "alerts", ["symbol_id", "timeframe"])


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_table("order_blocks")
    op.drop_table("liquidity_pools")
