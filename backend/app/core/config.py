from functools import lru_cache
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.constants import SUPPORTED_TIMEFRAMES, TIMEFRAMES
from app.market_data.source import spot_market_source


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    database_url: str = "postgresql+psycopg://elliott:elliott@localhost:5432/elliott_wave"
    redis_url: str = "redis://localhost:6379/0"
    binance_rest_base_url: str = "https://api.binance.com"
    binance_ws_base_url: str = "wss://stream.binance.com:9443/stream"
    default_symbols: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    default_timeframes: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(TIMEFRAMES))
    historical_candle_limit: int = Field(default=500, ge=10, le=1500)
    history_days_1m: int = Field(default=30, ge=1)
    history_days_5m: int = Field(default=90, ge=1)
    history_days_15m: int = Field(default=180, ge=1)
    history_days_1h: int = Field(default=365, ge=1)
    history_days_4h: int = Field(default=730, ge=1)
    history_backfill_rate_delay: float = Field(default=0.15, ge=0, le=5)
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    frontend_url: str = "http://localhost:5173"
    enable_startup_sync: bool = True
    analyze_historical_candles: bool = True
    enable_market_stream: bool = True
    environment: Literal["development", "test", "production"] = "development"
    binance_execution_enabled: bool = False
    binance_environment: Literal["testnet", "production"] = "testnet"
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    binance_max_clock_drift_ms: int = Field(default=1000, ge=0, le=60000)
    binance_testnet_base_url: str = "https://testnet.binance.vision"
    binance_production_base_url: str = "https://api.binance.com"
    execution_mode: Literal["disabled", "manual", "automatic_testnet", "live"] = "disabled"
    execution_require_manual_approval: bool = True
    credential_encryption_key: str = ""
    auth_cookie_secure: bool = False
    auth_session_hours: int = Field(default=24, ge=1, le=720)
    allow_production_orders: bool = False
    max_risk_per_trade_pct: Decimal = Field(default=Decimal("0.25"), gt=0, le=100)
    max_daily_loss_pct: Decimal = Field(default=Decimal("1.0"), gt=0, le=100)
    max_open_positions: int = Field(default=1, ge=1)
    max_symbol_exposure_pct: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    min_execution_confidence: Decimal = Field(default=Decimal("75"), ge=0, le=100)
    allowed_execution_symbols: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    allowed_execution_strategies: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["bullish_wave_3", "bullish_wave_5", "bullish_c_wave", "bullish_continuation", "bearish_wave_3", "bearish_wave_5", "bearish_c_wave", "bearish_continuation"])

    @field_validator("default_symbols", "default_timeframes", "allowed_execution_symbols", "allowed_execution_strategies", mode="before")
    @classmethod
    def split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("default_symbols")
    @classmethod
    def validate_symbols(cls, values: list[str]) -> list[str]:
        allowed = {"BTCUSDT", "ETHUSDT"}
        normalized = [v.upper() for v in values]
        if not normalized or not set(normalized) <= allowed:
            raise ValueError(f"symbols must be a non-empty subset of {sorted(allowed)}")
        return normalized

    @field_validator("default_timeframes")
    @classmethod
    def validate_timeframes(cls, values: list[str]) -> list[str]:
        if not values or not set(values) <= SUPPORTED_TIMEFRAMES:
            raise ValueError(f"timeframes must be a non-empty subset of {list(TIMEFRAMES)}")
        return values

    @model_validator(mode="after")
    def validate_execution_safety(self):
        spot_market_source(self.binance_rest_base_url, self.binance_ws_base_url)
        placeholders = {"changeme", "your_api_key", "your_api_secret", "placeholder", "test"}
        if self.binance_execution_enabled and (
            not self.binance_api_key or not self.binance_api_secret
            or self.binance_api_key.lower() in placeholders
            or self.binance_api_secret.lower() in placeholders
        ):
            raise ValueError("execution enabled with missing or placeholder Binance credentials")
        if self.binance_environment == "production" and self.binance_execution_enabled:
            if not (self.execution_mode == "live" and self.allow_production_orders):
                raise ValueError("production execution requires EXECUTION_MODE=live and ALLOW_PRODUCTION_ORDERS=true")
        if self.execution_mode == "automatic_testnet" and self.binance_environment != "testnet":
            raise ValueError("automatic execution is restricted to Binance Spot Testnet")
        if self.environment == "production" and not self.auth_cookie_secure:
            raise ValueError("AUTH_COOKIE_SECURE=true is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
