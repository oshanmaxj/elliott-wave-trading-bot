"""Deterministic Heikin Ashi signal data derived from immutable real candles."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

D = Decimal


@dataclass(frozen=True)
class HACandle:
    candle_id: int
    open_time: Any
    close_time: Any
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    real_high: Decimal
    real_low: Decimal
    real_close: Decimal

    @property
    def direction(self) -> str:
        return "bullish" if self.close > self.open else "bearish" if self.close < self.open else "neutral"

    @property
    def body(self) -> Decimal:
        return abs(self.close - self.open)


def derive_heikin_ashi(candles: Iterable[Any]) -> list[HACandle]:
    """Derive HA chronologically; initialize HA-open to the first real midpoint."""
    result: list[HACandle] = []
    for candle in sorted(candles, key=lambda row: (row.open_time, row.id)):
        if not candle.is_closed:
            continue
        real_open, real_high = D(candle.open), D(candle.high)
        real_low, real_close = D(candle.low), D(candle.close)
        ha_close = (real_open + real_high + real_low + real_close) / D("4")
        ha_open = ((real_open + real_close) / D("2")) if not result else ((result[-1].open + result[-1].close) / D("2"))
        result.append(HACandle(
            candle.id, candle.open_time, candle.close_time, ha_open,
            max(real_high, ha_open, ha_close), min(real_low, ha_open, ha_close),
            ha_close, real_high, real_low, real_close,
        ))
    return result


def true_ranges(candles: list[Any]) -> list[Decimal]:
    values: list[Decimal] = []
    previous = None
    for candle in candles:
        high, low = D(candle.high), D(candle.low)
        values.append(high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous)))
        previous = D(candle.close)
    return values


def atr_at(candles: list[Any], index: int, period: int = 14) -> Decimal | None:
    if index < period - 1:
        return None
    window = true_ranges(candles[: index + 1])[-period:]
    return sum(window, D("0")) / D(len(window))


def confirmed_reversal(
    ha: list[HACandle], real_candles: list[Any], confirmation_index: int, direction: str,
    *, pullback_min: int = 2, wick_body_max_ratio: Decimal = D("0.25"),
    body_atr_min_ratio: Decimal = D("0.25"), confirmation_required: bool = True,
    atr_period: int = 14,
) -> dict | None:
    """Return a signal only on the closed confirmation candle, using its real breakout."""
    reversal_index = confirmation_index - 1 if confirmation_required else confirmation_index
    if reversal_index < pullback_min or confirmation_index >= len(ha):
        return None
    wanted, prior = direction, "bearish" if direction == "bullish" else "bullish"
    reversal, confirmation = ha[reversal_index], ha[confirmation_index]
    if reversal.direction != wanted or any(row.direction != prior for row in ha[reversal_index-pullback_min:reversal_index]):
        return None
    body = reversal.body
    atr = atr_at(real_candles, reversal_index, atr_period)
    if not body or atr is None or body < atr * body_atr_min_ratio:
        return None
    wick = (min(reversal.open, reversal.close) - reversal.low) if direction == "bullish" else (reversal.high - max(reversal.open, reversal.close))
    if wick / body > wick_body_max_ratio:
        return None
    if confirmation_required and confirmation.direction != wanted:
        return None
    breakout = confirmation.real_high > reversal.real_high if direction == "bullish" else confirmation.real_low < reversal.real_low
    if not breakout:
        return None
    return {
        "direction": direction, "reversal_candle_id": reversal.candle_id,
        "confirmation_candle_id": confirmation.candle_id,
        "pullback_candle_ids": [row.candle_id for row in ha[reversal_index-pullback_min:reversal_index]],
        "body": str(body), "atr": str(atr), "wick_body_ratio": str(wick / body),
        "real_breakout_level": str(reversal.real_high if direction == "bullish" else reversal.real_low),
        "real_entry": str(confirmation.real_close), "confirmed_at": confirmation.close_time,
    }
