from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    database_url: str = "postgresql+psycopg://elliott:elliott@localhost:5432/elliott_wave"
    redis_url: str = "redis://localhost:6379/0"
    binance_rest_base_url: str = "https://fapi.binance.com"
    binance_ws_base_url: str = "wss://fstream.binance.com/stream"
    default_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    default_timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h"])
    historical_candle_limit: int = Field(default=500, ge=10, le=1500)
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    frontend_url: str = "http://localhost:5173"
    enable_startup_sync: bool = True
    enable_market_stream: bool = True
    environment: Literal["development", "test", "production"] = "development"

    @field_validator("default_symbols", "default_timeframes", mode="before")
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
        allowed = {"15m", "1h", "4h"}
        if not values or not set(values) <= allowed:
            raise ValueError(f"timeframes must be a non-empty subset of {sorted(allowed)}")
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()

