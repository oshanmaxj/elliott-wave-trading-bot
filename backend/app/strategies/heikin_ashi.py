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
    atr_period: int = 14, diagnostics: dict[str, Any] | None = None,
) -> dict | None:
    """Return a signal only on the closed confirmation candle, using its real breakout.

    If a `diagnostics` dict is passed in, it is filled in-place with the exact
    condition that failed (or "confirmed_reversal" on success) plus the
    relevant observed values and configured thresholds. This is for logging
    only - it never changes which candle this function returns.
    """
    if diagnostics is not None:
        diagnostics["pullback_min"] = pullback_min
        diagnostics["confirmation_required"] = confirmation_required
        diagnostics["wick_body_max_ratio"] = str(wick_body_max_ratio)
        diagnostics["body_atr_min_ratio"] = str(body_atr_min_ratio)
    reversal_index = confirmation_index - 1 if confirmation_required else confirmation_index
    if reversal_index < pullback_min or confirmation_index >= len(ha):
        if diagnostics is not None:
            diagnostics["reason"] = "insufficient_history"
        return None
    wanted, prior = direction, "bearish" if direction == "bullish" else "bullish"
    reversal, confirmation = ha[reversal_index], ha[confirmation_index]
    pullback_candles = ha[reversal_index - pullback_min:reversal_index]
    if diagnostics is not None:
        diagnostics["reversal_direction"] = reversal.direction
        diagnostics["confirmation_direction"] = confirmation.direction
        diagnostics["pullback_directions"] = [row.direction for row in pullback_candles]
        diagnostics["reversal_real_high"] = str(reversal.real_high)
        diagnostics["reversal_real_low"] = str(reversal.real_low)
        diagnostics["confirmation_real_high"] = str(confirmation.real_high)
        diagnostics["confirmation_real_low"] = str(confirmation.real_low)
    if reversal.direction != wanted:
        if diagnostics is not None:
            diagnostics["reason"] = "reversal_direction_failed"
        return None
    if any(row.direction != prior for row in pullback_candles):
        if diagnostics is not None:
            diagnostics["reason"] = "pullback_sequence_failed"
        return None
    body = reversal.body
    atr = atr_at(real_candles, reversal_index, atr_period)
    if diagnostics is not None:
        diagnostics["body"] = str(body)
        diagnostics["atr"] = str(atr) if atr is not None else None
        diagnostics["body_atr_ratio"] = str(body / atr) if atr else None
    if not body or atr is None or body < atr * body_atr_min_ratio:
        if diagnostics is not None:
            diagnostics["reason"] = "body_atr_failed"
        return None
    wick = (min(reversal.open, reversal.close) - reversal.low) if direction == "bullish" else (reversal.high - max(reversal.open, reversal.close))
    wick_body_ratio = wick / body
    if diagnostics is not None:
        diagnostics["wick"] = str(wick)
        diagnostics["wick_body_ratio"] = str(wick_body_ratio)
    if wick_body_ratio > wick_body_max_ratio:
        if diagnostics is not None:
            diagnostics["reason"] = "wick_body_ratio_failed"
        return None
    if confirmation_required and confirmation.direction != wanted:
        if diagnostics is not None:
            diagnostics["reason"] = "confirmation_direction_failed"
        return None
    breakout = confirmation.real_high > reversal.real_high if direction == "bullish" else confirmation.real_low < reversal.real_low
    if not breakout:
        if diagnostics is not None:
            diagnostics["reason"] = "real_price_breakout_failed"
        return None
    if diagnostics is not None:
        diagnostics["reason"] = "confirmed_reversal"
    return {
        "direction": direction, "reversal_candle_id": reversal.candle_id,
        "confirmation_candle_id": confirmation.candle_id,
        "pullback_candle_ids": [row.candle_id for row in pullback_candles],
        "body": str(body), "atr": str(atr), "wick_body_ratio": str(wick_body_ratio),
        "real_breakout_level": str(reversal.real_high if direction == "bullish" else reversal.real_low),
        "real_entry": str(confirmation.real_close), "confirmed_at": confirmation.close_time,
    }
