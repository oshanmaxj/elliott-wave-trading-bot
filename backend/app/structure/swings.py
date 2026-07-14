from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SwingCandidate:
    candle: Any
    swing_type: str
    price: Decimal
    strength: Decimal


def detect_confirmed_pivot(candles: list[Any], left_bars: int = 3, right_bars: int = 3) -> list[SwingCandidate]:
    required = left_bars + right_bars + 1
    if len(candles) < required or any(not c.is_closed for c in candles[-required:]):
        return []
    window = candles[-required:]
    pivot = window[left_bars]
    others = window[:left_bars] + window[left_bars + 1:]
    found: list[SwingCandidate] = []
    if all(pivot.high > c.high for c in others):
        prominence = min(float(pivot.high - max(c.high for c in window[:left_bars])), float(pivot.high - max(c.high for c in window[left_bars + 1:])))
        base = max(float(pivot.high - pivot.low), 1e-12)
        found.append(SwingCandidate(pivot, "high", pivot.high, Decimal(str(min(1.0, max(0.0, prominence / base))))))
    if all(pivot.low < c.low for c in others):
        prominence = min(float(min(c.low for c in window[:left_bars]) - pivot.low), float(min(c.low for c in window[left_bars + 1:]) - pivot.low))
        base = max(float(pivot.high - pivot.low), 1e-12)
        found.append(SwingCandidate(pivot, "low", pivot.low, Decimal(str(min(1.0, max(0.0, prominence / base))))))
    return found

