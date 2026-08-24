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


class BacktestRun(Base, TimestampMixin):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    starting_balance: Mapped[Decimal] = mapped_column(price_type)
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    total_setups: Mapped[int] = mapped_column(Integer, default=0)
    trades_taken: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    break_even: Mapped[int] = mapped_column(Integer, default=0)
    gross_profit: Mapped[Decimal] = mapped_column(price_type, default=0)
    gross_loss: Mapped[Decimal] = mapped_column(price_type, default=0)
    net_profit: Mapped[Decimal] = mapped_column(price_type, default=0)
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    win_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    expectancy: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    average_rr: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    sharpe_like_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    trade_setup_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_setups.id", ondelete="SET NULL"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(16))
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[Decimal] = mapped_column(price_type)
    stop_loss: Mapped[Decimal] = mapped_column(price_type)
    take_profit_1: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_2: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_3: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[Decimal] = mapped_column(price_type)
    exit_reason: Mapped[str] = mapped_column(String(32))
    risk_amount: Mapped[Decimal] = mapped_column(price_type)
    quantity: Mapped[Decimal] = mapped_column(price_type)
    fees: Mapped[Decimal] = mapped_column(price_type, default=0)
    slippage: Mapped[Decimal] = mapped_column(price_type, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(price_type)
    realized_r: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    mae: Mapped[Decimal] = mapped_column(price_type, default=0)
    mfe: Mapped[Decimal] = mapped_column(price_type, default=0)
    holding_bars: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Wave3HAResearchSignal(Base, TimestampMixin):
    """Auditable research output; deliberately has no execution-order relationship."""
    __tablename__ = "wave3_ha_research_signals"
    __table_args__ = (
        UniqueConstraint("event_fingerprint", "variant", name="uq_wave3_ha_event_variant"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_run_id: Mapped[int | None] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=True, index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    strategy: Mapped[str] = mapped_column(String(64), default="elliott_wave3_heikin_ashi_reversal", index=True)
    variant: Mapped[str] = mapped_column(String(1), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    event_fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    elliott_count_id: Mapped[int] = mapped_column(ForeignKey("elliott_wave_counts.id", ondelete="RESTRICT"), index=True)
    wave_point_ids_json: Mapped[list[int]] = mapped_column(JSON, default=list)
    artifact_ids_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 4), index=True)
    score_components_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reversal_candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id", ondelete="RESTRICT"))
    confirmation_candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id", ondelete="RESTRICT"))
    exit_ha_candle_id: Mapped[int | None] = mapped_column(ForeignKey("candles.id", ondelete="SET NULL"), nullable=True)
    real_entry: Mapped[Decimal] = mapped_column(price_type)
    real_stop: Mapped[Decimal] = mapped_column(price_type)
    real_exit: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    realized_r: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    mfe_r: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    mae_r: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    holding_seconds: Mapped[int] = mapped_column(Integer, default=0)
    volatility_regime: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    market_data_source: Mapped[str] = mapped_column(String(64), default="binance_production_spot_db")
    live_auto_execution_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class PaperAccount(Base, TimestampMixin):
    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    starting_balance: Mapped[Decimal] = mapped_column(price_type)
    balance: Mapped[Decimal] = mapped_column(price_type)
    equity: Mapped[Decimal] = mapped_column(price_type)
    realized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    max_equity: Mapped[Decimal] = mapped_column(price_type)
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=1)
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=3)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PaperPosition(Base, TimestampMixin):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True
    )
    trade_setup_id: Mapped[int] = mapped_column(
        ForeignKey("trade_setups.id", ondelete="RESTRICT"), index=True
    )
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    entry_price: Mapped[Decimal] = mapped_column(price_type)
    quantity: Mapped[Decimal] = mapped_column(price_type)
    initial_quantity: Mapped[Decimal] = mapped_column(price_type, default=0)
    risk_amount: Mapped[Decimal] = mapped_column(price_type, default=0)
    stop_loss: Mapped[Decimal] = mapped_column(price_type)
    tp1: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    tp2: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    tp3: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_price: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    realized_r: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    fees: Mapped[Decimal] = mapped_column(price_type, default=0)
    slippage: Mapped[Decimal] = mapped_column(price_type, default=0)
    taker_fee_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("0.05")
    )
    slippage_bps: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("2"))


class PaperForwardTrade(Base, TimestampMixin):
    """One immutable-geometry production-market simulation per canonical setup."""
    __tablename__ = "paper_forward_trades"
    __table_args__ = (UniqueConstraint("setup_id", name="uq_paper_forward_setup"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    setup_id: Mapped[int] = mapped_column(ForeignKey("trade_setups.id", ondelete="RESTRICT"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), index=True)
    simulated_entry: Mapped[Decimal] = mapped_column(price_type)
    entry_min: Mapped[Decimal] = mapped_column(price_type)
    entry_max: Mapped[Decimal] = mapped_column(price_type)
    stop_loss: Mapped[Decimal] = mapped_column(price_type)
    active_stop: Mapped[Decimal] = mapped_column(price_type)
    take_profit_1: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_2: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_3: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    next_target: Mapped[int] = mapped_column(Integer, default=1)
    initial_quantity: Mapped[Decimal] = mapped_column(price_type)
    remaining_quantity: Mapped[Decimal] = mapped_column(price_type)
    risk_amount: Mapped[Decimal] = mapped_column(price_type)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    exit_price: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    realized_r: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    fees: Mapped[Decimal] = mapped_column(price_type, default=0)
    fee_rate_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0.1"))
    status: Mapped[str] = mapped_column(String(32), index=True, default="waiting_entry")
    max_favorable_excursion: Mapped[Decimal] = mapped_column(price_type, default=0)
    max_adverse_excursion: Mapped[Decimal] = mapped_column(price_type, default=0)
    mfe_r: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    mae_r: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)
    holding_bars: Mapped[int] = mapped_column(Integer, default=0)
    is_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    market_data_source: Mapped[str] = mapped_column(String(64), default="binance_production_spot_db")


class ExchangeAccount(Base, TimestampMixin):
    __tablename__ = "exchange_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    environment: Mapped[str] = mapped_column(String(16), index=True)
    account_type: Mapped[str] = mapped_column(String(32), default="SPOT")
    masked_api_key: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    encrypted_api_secret: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )


class ExecutionOrder(Base, TimestampMixin):
    __tablename__ = "execution_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    environment: Mapped[str] = mapped_column(String(16), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), index=True)
    trade_setup_id: Mapped[int] = mapped_column(
        ForeignKey("trade_setups.id"), index=True
    )
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    setup_fingerprint: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    exchange_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    time_in_force: Mapped[str | None] = mapped_column(String(8), nullable=True)
    requested_quantity: Mapped[Decimal] = mapped_column(price_type)
    executed_quantity: Mapped[Decimal] = mapped_column(price_type, default=0)
    requested_price: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    average_fill_price: Mapped[Decimal | None] = mapped_column(
        price_type, nullable=True
    )
    quote_quantity: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    execution_state: Mapped[str] = mapped_column(String(32), index=True)
    raw_status_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ExecutionFill(Base):
    __tablename__ = "execution_fills"
    __table_args__ = (
        UniqueConstraint(
            "execution_order_id", "exchange_trade_id", name="uq_execution_fill_trade"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    execution_order_id: Mapped[int] = mapped_column(
        ForeignKey("execution_orders.id", ondelete="CASCADE"), index=True
    )
    exchange_trade_id: Mapped[str] = mapped_column(String(64))
    price: Mapped[Decimal] = mapped_column(price_type)
    quantity: Mapped[Decimal] = mapped_column(price_type)
    quote_quantity: Mapped[Decimal] = mapped_column(price_type)
    commission: Mapped[Decimal] = mapped_column(price_type, default=0)
    commission_asset: Mapped[str] = mapped_column(String(16))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LivePosition(Base, TimestampMixin):
    __tablename__ = "live_positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    environment: Mapped[str] = mapped_column(String(16), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), index=True)
    originating_trade_setup_id: Mapped[int] = mapped_column(
        ForeignKey("trade_setups.id"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    base_quantity: Mapped[Decimal] = mapped_column(price_type)
    remaining_quantity: Mapped[Decimal] = mapped_column(price_type)
    average_entry: Mapped[Decimal] = mapped_column(price_type)
    stop_loss: Mapped[Decimal] = mapped_column(price_type)
    take_profit_1: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_2: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    take_profit_3: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)
    protection_status: Mapped[str] = mapped_column(
        String(32), default="unprotected", index=True
    )
    realized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    total_fees: Mapped[Decimal] = mapped_column(price_type, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(price_type, nullable=True)


class ProtectiveOrder(Base, TimestampMixin):
    __tablename__ = "protective_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    live_position_id: Mapped[int] = mapped_column(
        ForeignKey("live_positions.id", ondelete="CASCADE"), index=True
    )
    environment: Mapped[str] = mapped_column(String(16), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), index=True)
    order_list_id: Mapped[str | None] = mapped_column(String(64), index=True)
    list_client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    stop_client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    take_profit_client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    stop_exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    take_profit_exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[Decimal] = mapped_column(price_type)
    stop_price: Mapped[Decimal] = mapped_column(price_type)
    take_profit_price: Mapped[Decimal] = mapped_column(price_type)
    status: Mapped[str] = mapped_column(String(32), index=True)
    raw_status_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    severity: Mapped[str] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(16))
    symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id"), nullable=True
    )
    trade_setup_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_setups.id"), nullable=True
    )
    execution_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_orders.id"), nullable=True
    )
    live_position_id: Mapped[int | None] = mapped_column(
        ForeignKey("live_positions.id"), nullable=True
    )
    message: Mapped[str] = mapped_column(String(1000))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class DailyRiskLedger(Base, TimestampMixin):
    __tablename__ = "daily_risk_ledgers"
    __table_args__ = (
        UniqueConstraint(
            "trading_date", "exchange", "environment", name="uq_daily_risk_ledger"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    trading_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exchange: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(16))
    starting_equity: Mapped[Decimal] = mapped_column(price_type)
    current_equity: Mapped[Decimal] = mapped_column(price_type)
    realized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(price_type, default=0)
    loss_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    orders_submitted: Mapped[int] = mapped_column(Integer, default=0)
    trades_opened: Mapped[int] = mapped_column(Integer, default=0)
    trades_closed: Mapped[int] = mapped_column(Integer, default=0)
    kill_switch_triggered: Mapped[bool] = mapped_column(Boolean, default=False)


class BotRuntimeState(Base, TimestampMixin):
    __tablename__ = "bot_runtime_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="stopped")
    environment: Mapped[str] = mapped_column(String(16), default="testnet")
    automatic_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    pause_new_entries: Mapped[bool] = mapped_column(Boolean, default=True)
    kill_switch_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_symbols_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled_timeframes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled_strategies_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    strategy_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stopped_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(16), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
