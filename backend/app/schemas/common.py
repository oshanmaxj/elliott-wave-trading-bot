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
