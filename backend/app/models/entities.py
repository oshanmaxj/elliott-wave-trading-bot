from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


price_type = Numeric(30, 12)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Symbol(Base, TimestampMixin):
    __tablename__ = "symbols"
    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    base_asset: Mapped[str] = mapped_column(String(16))
    quote_asset: Mapped[str] = mapped_column(String(16))
    market_type: Mapped[str] = mapped_column(String(32), default="usdt_perpetual")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Candle(Base, TimestampMixin):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id", "timeframe", "open_time", name="uq_candle_symbol_tf_open"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(price_type)
    high: Mapped[Decimal] = mapped_column(price_type)
    low: Mapped[Decimal] = mapped_column(price_type)
    close: Mapped[Decimal] = mapped_column(price_type)
    volume: Mapped[Decimal] = mapped_column(price_type)
    quote_volume: Mapped[Decimal] = mapped_column(price_type)
    trade_count: Mapped[int] = mapped_column(Integer)
    taker_buy_base_volume: Mapped[Decimal] = mapped_column(price_type)
    taker_buy_quote_volume: Mapped[Decimal] = mapped_column(price_type)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    symbol: Mapped[Symbol] = relationship()


class SwingPoint(Base):
    __tablename__ = "swing_points"
    __table_args__ = (
        UniqueConstraint("candle_id", "swing_type", name="uq_swing_candle_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id", ondelete="CASCADE"))
    swing_type: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal] = mapped_column(price_type)
    strength: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    confirmation_candles: Mapped[int] = mapped_column(Integer)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    candle: Mapped[Candle] = relationship()


class MarketStructureEvent(Base):
    __tablename__ = "market_structure_events"
    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "broken_swing_id",
            "confirmation_candle_id",
            name="uq_structure_event",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    event_type: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(16))
    broken_swing_id: Mapped[int] = mapped_column(
        ForeignKey("swing_points.id", ondelete="CASCADE")
    )
    confirmation_candle_id: Mapped[int] = mapped_column(
        ForeignKey("candles.id", ondelete="CASCADE")
    )
    break_price: Mapped[Decimal] = mapped_column(price_type)
    previous_trend: Mapped[str] = mapped_column(String(16))
    resulting_trend: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class FVGZone(Base, TimestampMixin):
    __tablename__ = "fvg_zones"
    __table_args__ = (
        UniqueConstraint(
            "first_candle_id",
            "middle_candle_id",
            "third_candle_id",
            "direction",
            name="uq_fvg_candles_direction",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    first_candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id"))
    middle_candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id"))
    third_candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id"))
    upper_price: Mapped[Decimal] = mapped_column(price_type)
    lower_price: Mapped[Decimal] = mapped_column(price_type)
    size_percentage: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    mitigation_percentage: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_touched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fully_mitigated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AnalysisSnapshot(Base):
    __tablename__ = "analysis_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id", "timeframe", "generated_at", name="uq_snapshot_generated"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    trend: Mapped[str] = mapped_column(String(16))
    latest_structure_event: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    active_fvg_count: Mapped[int] = mapped_column(Integer, default=0)
    indicator_values_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class LiquidityPool(Base):
    __tablename__ = "liquidity_pools"
    __table_args__ = (
        UniqueConstraint(
            "type", "first_swing_id", "second_swing_id", name="uq_liquidity_swing_pair"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    type: Mapped[str] = mapped_column(String(16), index=True)
    price: Mapped[Decimal] = mapped_column(price_type)
    strength: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    first_swing_id: Mapped[int] = mapped_column(
        ForeignKey("swing_points.id", ondelete="CASCADE")
    )
    second_swing_id: Mapped[int] = mapped_column(
        ForeignKey("swing_points.id", ondelete="CASCADE")
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    swept_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class OrderBlock(Base):
    __tablename__ = "order_blocks"
    __table_args__ = (UniqueConstraint("bos_event_id", name="uq_order_block_bos"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id", ondelete="CASCADE"))
    top_price: Mapped[Decimal] = mapped_column(price_type)
    bottom_price: Mapped[Decimal] = mapped_column(price_type)
    bos_event_id: Mapped[int] = mapped_column(
        ForeignKey("market_structure_events.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    mitigation_percent: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    first_touched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fully_mitigated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("type", "source_type", "source_id", name="uq_alert_source"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    message: Mapped[str] = mapped_column(String(500))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class LiquiditySweep(Base, TimestampMixin):
    __tablename__ = "liquidity_sweeps"
    __table_args__ = (
        UniqueConstraint(
            "liquidity_pool_id", "sweep_candle_id", name="uq_sweep_pool_candle"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    liquidity_pool_id: Mapped[int] = mapped_column(
        ForeignKey("liquidity_pools.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16), index=True)
    sweep_type: Mapped[str] = mapped_column(String(32), index=True)
    sweep_candle_id: Mapped[int] = mapped_column(
        ForeignKey("candles.id", ondelete="CASCADE")
    )
    confirmation_candle_id: Mapped[int | None] = mapped_column(
        ForeignKey("candles.id", ondelete="SET NULL"), nullable=True
    )
    liquidity_price: Mapped[Decimal] = mapped_column(price_type)
    extreme_price: Mapped[Decimal] = mapped_column(price_type)
    reclaimed_price: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    penetration_percentage: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    rejection_strength: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    volume_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TradeSetup(Base, TimestampMixin):
    __tablename__ = "trade_setups"
    __table_args__ = (
        UniqueConstraint(
            "strategy",
            "structure_event_id",
            "setup_timeframe",
            name="uq_setup_strategy_structure_tf",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    higher_timeframe: Mapped[str] = mapped_column(String(8))
    setup_timeframe: Mapped[str] = mapped_column(String(8), index=True)
    entry_timeframe: Mapped[str] = mapped_column(String(8))
    liquidity_sweep_id: Mapped[int | None] = mapped_column(
        ForeignKey("liquidity_sweeps.id", ondelete="SET NULL"), nullable=True
    )
    structure_event_id: Mapped[int] = mapped_column(
        ForeignKey("market_structure_events.id", ondelete="CASCADE")
    )
    fvg_zone_id: Mapped[int | None] = mapped_column(
        ForeignKey("fvg_zones.id", ondelete="SET NULL"), nullable=True
    )
    order_block_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_blocks.id", ondelete="SET NULL"), nullable=True
    )
    elliott_wave_count_id: Mapped[int | None] = mapped_column(
        ForeignKey("elliott_wave_counts.id", ondelete="SET NULL"), nullable=True
    )
    entry_min: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    entry_max: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    preferred_entry: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    invalidation_price: Mapped[Decimal | None] = mapped_column(
        price_type, nullable=True
    )
    take_profit_1: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_2: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_3: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    risk_reward_1: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    risk_reward_2: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    risk_reward_3: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    score_breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    setup_conditions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rejection_reasons_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ElliottWaveCount(Base, TimestampMixin):
    __tablename__ = "elliott_wave_counts"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id",
            "timeframe",
            "pattern_type",
            "start_candle_id",
            "end_candle_id",
            name="uq_wave_count_equivalent",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    degree: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    pattern_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    start_candle_id: Mapped[int] = mapped_column(
        ForeignKey("candles.id", ondelete="CASCADE")
    )
    end_candle_id: Mapped[int] = mapped_column(
        ForeignKey("candles.id", ondelete="CASCADE")
    )
    invalidation_price: Mapped[Decimal] = mapped_column(price_type)
    projected_target_min: Mapped[Decimal | None] = mapped_column(
        price_type, nullable=True
    )
    projected_target_max: Mapped[Decimal | None] = mapped_column(
        price_type, nullable=True
    )
    rules_passed_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    rules_failed_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    fibonacci_scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    structure_confirmation_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict
    )
    liquidity_confirmation_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    points: Mapped[list["ElliottWavePoint"]] = relationship(
        cascade="all, delete-orphan", order_by="ElliottWavePoint.sequence_number"
    )


class ElliottWavePoint(Base):
    __tablename__ = "elliott_wave_points"
    __table_args__ = (
        UniqueConstraint(
            "wave_count_id", "sequence_number", name="uq_wave_point_sequence"
        ),
        UniqueConstraint("wave_count_id", "wave_label", name="uq_wave_point_label"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    wave_count_id: Mapped[int] = mapped_column(
        ForeignKey("elliott_wave_counts.id", ondelete="CASCADE"), index=True
    )
    wave_label: Mapped[str] = mapped_column(String(4))
    sequence_number: Mapped[int] = mapped_column(Integer)
    swing_point_id: Mapped[int] = mapped_column(
        ForeignKey("swing_points.id", ondelete="CASCADE")
    )
    candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id", ondelete="CASCADE"))
    price: Mapped[Decimal] = mapped_column(price_type)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fibonacci_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    duration_bars: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class BotLog(Base):
    __tablename__ = "bot_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    service: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(1000))
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Setting(Base, TimestampMixin):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value_json: Mapped[Any] = mapped_column(JSON)
