"""liquidity sweep and paper trade setup lifecycle

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(30, 12)


def upgrade() -> None:
    op.add_column(
        "liquidity_pools",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.create_index("ix_liquidity_pools_status", "liquidity_pools", ["status"])

    op.create_table(
        "liquidity_sweeps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("liquidity_pool_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("sweep_type", sa.String(32), nullable=False),
        sa.Column("sweep_candle_id", sa.Integer(), nullable=False),
        sa.Column("confirmation_candle_id", sa.Integer(), nullable=True),
        sa.Column("liquidity_price", PRICE, nullable=False),
        sa.Column("extreme_price", PRICE, nullable=False),
        sa.Column("reclaimed_price", PRICE, nullable=True),
        sa.Column("penetration_percentage", sa.Numeric(12, 6), nullable=False),
        sa.Column("rejection_strength", sa.Numeric(8, 4), nullable=False),
        sa.Column("volume_ratio", sa.Numeric(12, 6), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("confidence_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["liquidity_pool_id"], ["liquidity_pools.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sweep_candle_id"], ["candles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_candle_id"], ["candles.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "liquidity_pool_id", "sweep_candle_id", name="uq_sweep_pool_candle"
        ),
    )
    for column in (
        "symbol_id",
        "timeframe",
        "liquidity_pool_id",
        "direction",
        "sweep_type",
        "status",
        "detected_at",
    ):
        op.create_index(f"ix_liquidity_sweeps_{column}", "liquidity_sweeps", [column])

    op.create_table(
        "trade_setups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("higher_timeframe", sa.String(8), nullable=False),
        sa.Column("setup_timeframe", sa.String(8), nullable=False),
        sa.Column("entry_timeframe", sa.String(8), nullable=False),
        sa.Column("liquidity_sweep_id", sa.Integer(), nullable=True),
        sa.Column("structure_event_id", sa.Integer(), nullable=False),
        sa.Column("fvg_zone_id", sa.Integer(), nullable=True),
        sa.Column("order_block_id", sa.Integer(), nullable=True),
        sa.Column("entry_min", PRICE, nullable=True),
        sa.Column("entry_max", PRICE, nullable=True),
        sa.Column("preferred_entry", PRICE, nullable=True),
        sa.Column("stop_loss", PRICE, nullable=True),
        sa.Column("invalidation_price", PRICE, nullable=True),
        sa.Column("take_profit_1", PRICE, nullable=True),
        sa.Column("take_profit_2", PRICE, nullable=True),
        sa.Column("take_profit_3", PRICE, nullable=True),
        sa.Column("risk_reward_1", sa.Numeric(12, 6), nullable=True),
        sa.Column("risk_reward_2", sa.Numeric(12, 6), nullable=True),
        sa.Column("risk_reward_3", sa.Numeric(12, 6), nullable=True),
        sa.Column("confidence_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("score_breakdown_json", sa.JSON(), nullable=False),
        sa.Column("setup_conditions_json", sa.JSON(), nullable=False),
        sa.Column("rejection_reasons_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["liquidity_sweep_id"], ["liquidity_sweeps.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["structure_event_id"],
            ["market_structure_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["fvg_zone_id"], ["fvg_zones.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["order_block_id"], ["order_blocks.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "strategy",
            "structure_event_id",
            "setup_timeframe",
            name="uq_setup_strategy_structure_tf",
        ),
    )
    for column in (
        "symbol_id",
        "direction",
        "strategy",
        "status",
        "setup_timeframe",
        "expires_at",
        "detected_at",
    ):
        op.create_index(f"ix_trade_setups_{column}", "trade_setups", [column])


def downgrade() -> None:
    op.drop_table("trade_setups")
    op.drop_table("liquidity_sweeps")
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"] for item in inspector.get_indexes("liquidity_pools")}
    if "ix_liquidity_pools_status" in indexes:
        op.drop_index("ix_liquidity_pools_status", table_name="liquidity_pools")
    columns = {item["name"] for item in inspector.get_columns("liquidity_pools")}
    if "status" in columns:
        op.drop_column("liquidity_pools", "status")
