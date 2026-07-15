"""initial schema

Revision ID: 0001
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

PRICE = sa.Numeric(30, 12)


def upgrade() -> None:
    op.create_table(
        "symbols",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("base_asset", sa.String(16), nullable=False),
        sa.Column("quote_asset", sa.String(16), nullable=False),
        sa.Column("market_type", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_symbols_symbol", "symbols", ["symbol"], unique=True)

    op.create_table(
        "candles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", PRICE, nullable=False),
        sa.Column("quote_volume", PRICE, nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("taker_buy_base_volume", PRICE, nullable=False),
        sa.Column("taker_buy_quote_volume", PRICE, nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "symbol_id", "timeframe", "open_time", name="uq_candle_symbol_tf_open"
        ),
    )
    for column in ("symbol_id", "timeframe", "open_time", "is_closed"):
        op.create_index(f"ix_candles_{column}", "candles", [column])

    op.create_table(
        "swing_points",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("candle_id", sa.Integer(), nullable=False),
        sa.Column("swing_type", sa.String(16), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("strength", sa.Numeric(8, 4), nullable=False),
        sa.Column("confirmation_candles", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candle_id"], ["candles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("candle_id", "swing_type", name="uq_swing_candle_type"),
    )
    op.create_index("ix_swing_points_symbol_id", "swing_points", ["symbol_id"])
    op.create_index("ix_swing_points_timeframe", "swing_points", ["timeframe"])

    op.create_table(
        "market_structure_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("broken_swing_id", sa.Integer(), nullable=False),
        sa.Column("confirmation_candle_id", sa.Integer(), nullable=False),
        sa.Column("break_price", PRICE, nullable=False),
        sa.Column("previous_trend", sa.String(16), nullable=False),
        sa.Column("resulting_trend", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 4), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["broken_swing_id"], ["swing_points.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_candle_id"], ["candles.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "event_type",
            "broken_swing_id",
            "confirmation_candle_id",
            name="uq_structure_event",
        ),
    )
    op.create_index(
        "ix_market_structure_events_symbol_id",
        "market_structure_events",
        ["symbol_id"],
    )
    op.create_index(
        "ix_market_structure_events_timeframe",
        "market_structure_events",
        ["timeframe"],
    )

    op.create_table(
        "fvg_zones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("first_candle_id", sa.Integer(), nullable=False),
        sa.Column("middle_candle_id", sa.Integer(), nullable=False),
        sa.Column("third_candle_id", sa.Integer(), nullable=False),
        sa.Column("upper_price", PRICE, nullable=False),
        sa.Column("lower_price", PRICE, nullable=False),
        sa.Column("size_percentage", sa.Numeric(12, 6), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("mitigation_percentage", sa.Numeric(8, 4), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_touched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fully_mitigated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["first_candle_id"], ["candles.id"]),
        sa.ForeignKeyConstraint(["middle_candle_id"], ["candles.id"]),
        sa.ForeignKeyConstraint(["third_candle_id"], ["candles.id"]),
        sa.UniqueConstraint(
            "first_candle_id",
            "middle_candle_id",
            "third_candle_id",
            "direction",
            name="uq_fvg_candles_direction",
        ),
    )
    for column in ("symbol_id", "timeframe", "status"):
        op.create_index(f"ix_fvg_zones_{column}", "fvg_zones", [column])

    op.create_table(
        "analysis_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("trend", sa.String(16), nullable=False),
        sa.Column("latest_structure_event", sa.String(16), nullable=True),
        sa.Column("active_fvg_count", sa.Integer(), nullable=False),
        sa.Column("indicator_values_json", sa.JSON(), nullable=False),
        sa.Column("confidence_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "symbol_id", "timeframe", "generated_at", name="uq_snapshot_generated"
        ),
    )
    for column in ("symbol_id", "timeframe", "generated_at"):
        op.create_index(
            f"ix_analysis_snapshots_{column}", "analysis_snapshots", [column]
        )

    op.create_table(
        "bot_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("message", sa.String(1000), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("level", "service", "created_at"):
        op.create_index(f"ix_bot_logs_{column}", "bot_logs", [column])

    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("bot_logs")
    op.drop_table("analysis_snapshots")
    op.drop_table("fvg_zones")
    op.drop_table("market_structure_events")
    op.drop_table("swing_points")
    op.drop_table("candles")
    op.drop_table("symbols")
