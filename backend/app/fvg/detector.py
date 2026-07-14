from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class FVGConfig:
    min_atr_fraction: float = 0.15
    min_body_size: float = 0.0
    min_body_to_range_ratio: float = 0.5
    require_volume_confirmation: bool = False
    require_structure_event: bool = False


@dataclass(frozen=True)
class FVGSignal:
    direction: str
    lower_price: Decimal
    upper_price: Decimal
    size_percentage: Decimal


def detect_fvg(candles: list[Any], atr: float | None, volume_ratio: float | None = None, has_structure_event: bool = False, config: FVGConfig | None = None) -> FVGSignal | None:
    config = config or FVGConfig()
    if len(candles) < 3:
        return None
    first, middle, third = candles[-3:]
    body = abs(float(middle.close - middle.open))
    candle_range = float(middle.high - middle.low)
    if candle_range <= 0 or body < config.min_body_size or body / candle_range < config.min_body_to_range_ratio:
        return None
    if config.require_volume_confirmation and (volume_ratio is None or volume_ratio < 1.0):
        return None
    if config.require_structure_event and not has_structure_event:
        return None
    direction: str | None = None
    lower = upper = Decimal(0)
    if first.high < third.low and middle.close > middle.open:
        direction, lower, upper = "bullish", first.high, third.low
    elif first.low > third.high and middle.close < middle.open:
        direction, lower, upper = "bearish", third.high, first.low
    if direction is None:
        return None
    gap = float(upper - lower)
    if atr is not None and gap < atr * config.min_atr_fraction:
        return None
    midpoint = float((upper + lower) / 2)
    size_pct = Decimal(str(0 if midpoint == 0 else gap / midpoint * 100))
    return FVGSignal(direction, lower, upper, size_pct)


def mitigation_update(zone: Any, candle: Any) -> tuple[str, Decimal]:
    lower, upper = Decimal(zone.lower_price), Decimal(zone.upper_price)
    size = upper - lower
    if size <= 0:
        return zone.status, Decimal(zone.mitigation_percentage)
    if zone.direction == "bullish":
        if candle.close < lower:
            return "invalidated", Decimal("100")
        penetration = max(Decimal(0), upper - Decimal(candle.low))
    else:
        if candle.close > upper:
            return "invalidated", Decimal("100")
        penetration = max(Decimal(0), Decimal(candle.high) - lower)
    percentage = min(Decimal("100"), penetration / size * 100)
    if percentage >= 100:
        return "fully_mitigated", percentage
    if percentage > 0:
        return "partially_mitigated", percentage
    return "active", Decimal(0)
