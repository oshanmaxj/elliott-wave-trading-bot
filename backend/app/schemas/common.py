from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constants import SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SymbolOut(ORMModel):
    id: int
    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    market_type: str
    is_active: bool


class CandleData(BaseModel):
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    is_closed: bool


class CandleOut(CandleData, ORMModel):
    id: int
    symbol_id: int
    timeframe: str


class SwingOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    candle_id: int
    swing_type: str
    price: Decimal
    strength: Decimal
    confirmation_candles: int
    detected_at: datetime
    metadata_json: dict[str, Any]


class StructureOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    event_type: str
    direction: str
    broken_swing_id: int
    confirmation_candle_id: int
    break_price: Decimal
    previous_trend: str
    resulting_trend: str
    confidence: Decimal
    detected_at: datetime
    metadata_json: dict[str, Any]


class FVGOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    direction: str
    first_candle_id: int
    middle_candle_id: int
    third_candle_id: int
    upper_price: Decimal
    lower_price: Decimal
    size_percentage: Decimal
    status: str
    mitigation_percentage: Decimal
    detected_at: datetime
    first_touched_at: datetime | None
    fully_mitigated_at: datetime | None
    invalidated_at: datetime | None
    metadata_json: dict[str, Any]


class AnalysisOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    trend: str
    latest_structure_event: str | None
    active_fvg_count: int
    indicator_values_json: dict[str, Any]
    confidence_score: Decimal
    generated_at: datetime


class LiquidityOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    type: str
    price: Decimal
    strength: Decimal
    first_swing_id: int
    second_swing_id: int
    detected_at: datetime
    swept_at: datetime | None
    status: str
    metadata_json: dict[str, Any]


class OrderBlockOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    direction: str
    candle_id: int
    top_price: Decimal
    bottom_price: Decimal
    bos_event_id: int
    status: str
    mitigation_percent: Decimal
    detected_at: datetime
    first_touched_at: datetime | None
    fully_mitigated_at: datetime | None
    invalidated_at: datetime | None


class AlertOut(ORMModel):
    id: int
    type: str
    symbol_id: int
    timeframe: str
    message: str
    created_at: datetime


class LiquiditySweepOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    liquidity_pool_id: int
    direction: str
    sweep_type: str
    sweep_candle_id: int
    confirmation_candle_id: int | None
    liquidity_price: Decimal
    extreme_price: Decimal
    reclaimed_price: Decimal | None
    penetration_percentage: Decimal
    rejection_strength: Decimal
    volume_ratio: Decimal | None
    status: str
    confidence_score: Decimal
    detected_at: datetime
    confirmed_at: datetime | None
    invalidated_at: datetime | None
    metadata_json: dict[str, Any]


class TradeSetupOut(ORMModel):
    id: int
    symbol_id: int
    direction: str
    strategy: str
    status: str
    higher_timeframe: str
    setup_timeframe: str
    entry_timeframe: str
    liquidity_sweep_id: int | None
    structure_event_id: int
    fvg_zone_id: int | None
    order_block_id: int | None
    elliott_wave_count_id: int | None
    entry_min: Decimal | None
    entry_max: Decimal | None
    preferred_entry: Decimal | None
    stop_loss: Decimal | None
    invalidation_price: Decimal | None
    take_profit_1: Decimal | None
    take_profit_2: Decimal | None
    take_profit_3: Decimal | None
    risk_reward_1: Decimal | None
    risk_reward_2: Decimal | None
    risk_reward_3: Decimal | None
    confidence_score: Decimal
    score_breakdown_json: dict[str, Any]
    setup_conditions_json: dict[str, Any]
    rejection_reasons_json: list[str]
    expires_at: datetime
    detected_at: datetime
    triggered_at: datetime | None
    invalidated_at: datetime | None


class TradeSetupSummary(BaseModel):
    watching_count: int
    ready_count: int
    bullish_count: int
    bearish_count: int
    latest_ready_setup: TradeSetupOut | None
    average_confidence: float


class ElliottWavePointOut(ORMModel):
    id: int
    wave_label: str
    sequence_number: int
    swing_point_id: int
    candle_id: int
    price: Decimal
    timestamp: datetime
    fibonacci_ratio: Decimal | None
    duration_bars: int
    metadata_json: dict[str, Any]


class ElliottWaveCountOut(ORMModel):
    id: int
    symbol_id: int
    timeframe: str
    degree: str
    direction: str
    pattern_type: str
    status: str
    rank: int
    confidence_score: Decimal
    start_candle_id: int
    end_candle_id: int
    invalidation_price: Decimal
    projected_target_min: Decimal | None
    projected_target_max: Decimal | None
    rules_passed_json: list[str]
    rules_failed_json: list[str]
    fibonacci_scores_json: dict[str, Any]
    structure_confirmation_json: dict[str, Any]
    liquidity_confirmation_json: dict[str, Any]
    metadata_json: dict[str, Any]
    detected_at: datetime
    confirmed_at: datetime | None
    completed_at: datetime | None
    invalidated_at: datetime | None
    points: list[ElliottWavePointOut]


class ElliottRecalculateRequest(BaseModel):
    symbol: str
    timeframe: str
    rebuild: bool = False

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        value = value.upper()
        if value not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported symbol")
        return value

    @field_validator("timeframe")
    @classmethod
    def valid_timeframe(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        return value


class PremiumDiscountOut(BaseModel):
    swing_high: Decimal
    swing_low: Decimal
    equilibrium: Decimal
    premium: dict[str, Decimal]
    discount: dict[str, Decimal]


class MarketBiasOut(BaseModel):
    symbol: str
    timeframes: dict[str, str]
    score: int
    label: str
    aligned: bool


class StructureScoreOut(BaseModel):
    symbol: str
    timeframe: str
    score: int
    label: str


class BotLogOut(ORMModel):
    id: int
    level: str
    service: str
    event_type: str
    message: str
    context_json: dict[str, Any]
    created_at: datetime


class SyncRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: datetime | None = None
    end_time: datetime | None = None

    @field_validator("symbol")
    @classmethod
    def symbol_supported(cls, value: str) -> str:
        value = value.upper()
        if value not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported symbol")
        return value

    @field_validator("timeframe")
    @classmethod
    def timeframe_supported(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        return value


class AnalysisBackfillRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int | None = Field(500, ge=1, le=1500)
    rebuild: bool = False

    @field_validator("symbol")
    @classmethod
    def symbol_supported(cls, value: str) -> str:
        value = value.upper()
        if value not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported symbol")
        return value

    @field_validator("timeframe")
    @classmethod
    def timeframe_supported(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        return value


class AnalysisBackfillReport(BaseModel):
    symbol: str
    timeframe: str
    total_candles: int
    processed: int
    skipped: int
    failed: int
    events_generated: int
    started_at: datetime
    completed_at: datetime
    duration_ms: float


class AnalysisBackfillStatusOut(BaseModel):
    running: bool
    symbol: str | None
    timeframe: str | None
    total_candles: int
    processed_candles: int
    failed_candles: int
    progress_percentage: float
    started_at: datetime | None
    last_completed_at: datetime | None


class RuntimeSettings(BaseModel):
    enabled_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    enabled_timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h"])
    swing_left_bars: int = Field(3, ge=1, le=20)
    swing_right_bars: int = Field(3, ge=1, le=20)
    wick_break_allowed: bool = False
    minimum_fvg_atr_size: float = Field(0.15, ge=0, le=10)
    fvg_volume_confirmation: bool = False
    structure_confidence_threshold: float = Field(0.5, ge=0, le=1)
    liquidity_tolerance_percentage: float = Field(0.1, ge=0.001, le=5)
    sweep_minimum_penetration_percentage: float = Field(0.02, ge=0, le=10)
    sweep_maximum_penetration_percentage: float = Field(1.5, ge=0.01, le=20)
    sweep_confirmation_candles: int = Field(2, ge=0, le=20)
    sweep_minimum_wick_ratio: float = Field(0.35, ge=0, le=1)
    sweep_minimum_volume_ratio: float | None = Field(None, ge=0)
    sweep_expiry_candles: int = Field(8, ge=1, le=100)
    sweep_liquidity_strength_threshold: float = Field(0.3, ge=0, le=1)
    sweep_require_closed_confirmation: bool = True
    sweep_allow_same_candle_confirmation: bool = True
    minimum_sweep_confidence: float = Field(55, ge=0, le=100)
    minimum_setup_confidence: float = Field(60, ge=0, le=100)
    setup_expiry_candles: int = Field(24, ge=1, le=500)
    stop_loss_atr_buffer: float = Field(0.25, ge=0, le=10)
    minimum_reward_to_risk: float = Field(1.5, ge=0.1, le=20)
    counter_trend_setups_enabled: bool = False
    counter_trend_minimum_confidence: float = Field(80, ge=0, le=100)
    chart_sweep_display: bool = True
    chart_setup_display: bool = True
    elliott_fibonacci_tolerance: float = Field(0.15, ge=0.01, le=0.5)
    elliott_max_alternate_counts: int = Field(2, ge=0, le=5)
    elliott_allow_zigzag_truncation: bool = False
    elliott_minimum_confidence: float = Field(55, ge=0, le=100)
    elliott_wave_5_risk_factor: float = Field(0.6, ge=0.1, le=1)

    @field_validator("enabled_symbols")
    @classmethod
    def symbols_supported(cls, values: list[str]) -> list[str]:
        values = [v.upper() for v in values]
        if not values or not set(values) <= SUPPORTED_SYMBOLS:
            raise ValueError("unsupported symbol")
        return values

    @field_validator("enabled_timeframes")
    @classmethod
    def timeframes_supported(cls, values: list[str]) -> list[str]:
        if not values or not set(values) <= SUPPORTED_TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        return values
