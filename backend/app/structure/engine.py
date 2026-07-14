from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def classify_trend(swings: list[Any]) -> str:
    highs = [s for s in swings if s.swing_type == "high"][-2:]
    lows = [s for s in swings if s.swing_type == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "undefined"
    if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
        return "bullish"
    if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
        return "bearish"
    return "range"


def swing_labels(swings: list[Any]) -> dict[int, str]:
    labels: dict[int, str] = {}
    previous: dict[str, Any] = {}
    for swing in swings:
        prior = previous.get(swing.swing_type)
        if prior:
            if swing.swing_type == "high":
                labels[swing.id] = "HH" if swing.price > prior.price else "LH"
            else:
                labels[swing.id] = "HL" if swing.price > prior.price else "LL"
        previous[swing.swing_type] = swing
    return labels


@dataclass(frozen=True)
class StructureSignal:
    event_type: str
    direction: str
    broken_swing: Any
    previous_trend: str
    resulting_trend: str
    confidence: Decimal


def detect_structure_break(candle: Any, previous_candle: Any | None, swings: list[Any], wick_break_allowed: bool = False) -> StructureSignal | None:
    trend = classify_trend(swings)
    highs = [s for s in swings if s.swing_type == "high" and s.candle.open_time < candle.open_time]
    lows = [s for s in swings if s.swing_type == "low" and s.candle.open_time < candle.open_time]
    if not highs or not lows:
        return None
    high, low = highs[-1], lows[-1]
    current_high_value = candle.high if wick_break_allowed else candle.close
    current_low_value = candle.low if wick_break_allowed else candle.close
    prior_high_value = (previous_candle.high if wick_break_allowed else previous_candle.close) if previous_candle else None
    prior_low_value = (previous_candle.low if wick_break_allowed else previous_candle.close) if previous_candle else None
    if current_high_value > high.price and (prior_high_value is None or prior_high_value <= high.price):
        event = "CHoCH" if trend == "bearish" else "BOS"
        confidence = Decimal("0.85") if trend in {"bearish", "bullish"} else Decimal("0.65")
        return StructureSignal(event, "bullish", high, trend, "bullish", confidence)
    if current_low_value < low.price and (prior_low_value is None or prior_low_value >= low.price):
        event = "CHoCH" if trend == "bullish" else "BOS"
        confidence = Decimal("0.85") if trend in {"bearish", "bullish"} else Decimal("0.65")
        return StructureSignal(event, "bearish", low, trend, "bearish", confidence)
    return None

