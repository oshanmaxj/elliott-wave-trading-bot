from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class LiquiditySignal:
    type: str
    pattern: str
    price: Decimal
    strength: Decimal
    first_swing: Any
    second_swing: Any


def detect_liquidity(swings: list[Any], tolerance_percentage: float = 0.1) -> LiquiditySignal | None:
    if len(swings) < 2:
        return None
    second = swings[-1]
    prior = [s for s in swings[:-1] if s.swing_type == second.swing_type]
    if not prior:
        return None
    first = prior[-1]
    midpoint = (abs(first.price) + abs(second.price)) / 2
    tolerance = midpoint * Decimal(str(tolerance_percentage / 100))
    if abs(first.price - second.price) > tolerance:
        return None
    pool_type = "BSL" if second.swing_type == "high" else "SSL"
    pattern = "EQH" if second.swing_type == "high" else "EQL"
    price = max(first.price, second.price) if pool_type == "BSL" else min(first.price, second.price)
    closeness = Decimal("1") if tolerance == 0 else max(Decimal("0"), Decimal("1") - abs(first.price - second.price) / tolerance)
    return LiquiditySignal(pool_type, pattern, price, closeness, first, second)


@dataclass(frozen=True)
class OrderBlockSignal:
    direction: str
    candle: Any
    top_price: Decimal
    bottom_price: Decimal


def detect_order_block(candles: list[Any], structure_event: Any | None) -> OrderBlockSignal | None:
    if not structure_event or structure_event.event_type != "BOS" or len(candles) < 2:
        return None
    direction = structure_event.direction
    for candidate in reversed(candles[:-1]):
        bearish = candidate.close < candidate.open
        bullish = candidate.close > candidate.open
        if (direction == "bullish" and bearish) or (direction == "bearish" and bullish):
            return OrderBlockSignal(direction, candidate, candidate.high, candidate.low)
    return None


def order_block_mitigation(block: Any, candle: Any) -> tuple[str, Decimal]:
    top, bottom = Decimal(block.top_price), Decimal(block.bottom_price)
    size = top - bottom
    if size <= 0:
        return block.status, Decimal(block.mitigation_percent)
    if block.direction == "bullish":
        if candle.close < bottom:
            return "invalidated", Decimal("100")
        penetration = max(Decimal("0"), top - Decimal(candle.low))
    else:
        if candle.close > top:
            return "invalidated", Decimal("100")
        penetration = max(Decimal("0"), Decimal(candle.high) - bottom)
    percentage = min(Decimal("100"), penetration / size * 100)
    if percentage >= 100:
        return "fully_mitigated", percentage
    return ("partially_mitigated", percentage) if percentage > 0 else ("active", Decimal("0"))


def premium_discount(swings: list[Any]) -> dict[str, Any] | None:
    highs = [s for s in swings if s.swing_type == "high"]
    lows = [s for s in swings if s.swing_type == "low"]
    if not highs or not lows:
        return None
    high, low = highs[-1].price, lows[-1].price
    if high <= low:
        return None
    return {"swing_high": high, "swing_low": low, "equilibrium": (high + low) / 2, "premium": {"bottom": (high + low) / 2, "top": high}, "discount": {"bottom": low, "top": (high + low) / 2}}


def structure_score(trend: str, latest_event: Any | None, liquidity_count: int, order_block_count: int, fvg_count: int, indicators: dict[str, Any]) -> dict[str, Any]:
    score = 50
    score += 15 if trend == "bullish" else -15 if trend == "bearish" else 0
    if latest_event:
        sign = 1 if latest_event.direction == "bullish" else -1
        score += sign * (12 if latest_event.event_type == "BOS" else 8)
    score += min(liquidity_count, 2) * 2
    score += min(order_block_count, 2) * 3
    score += min(fvg_count, 2) * 2
    ema20, ema50, ema200 = (indicators.get(key) for key in ("ema20", "ema50", "ema200"))
    if all(value is not None for value in (ema20, ema50, ema200)):
        score += 10 if ema20 > ema50 > ema200 else -10 if ema20 < ema50 < ema200 else 0
    score = max(0, min(100, score))
    label = "Strong Bullish" if score >= 85 else "Bullish" if score >= 70 else "Neutral" if score >= 50 else "Bearish" if score >= 30 else "Strong Bearish"
    return {"score": score, "label": label}


def multi_timeframe_bias(trends: dict[str, str]) -> dict[str, Any]:
    weights = {"4h": 3, "1h": 2, "15m": 1}
    value = sum(weights[tf] * (1 if trends.get(tf) == "bullish" else -1 if trends.get(tf) == "bearish" else 0) for tf in weights)
    aligned = len({trends.get(tf) for tf in weights}) == 1 and trends.get("4h") in {"bullish", "bearish"}
    label = ("Strong Bullish Alignment" if value > 0 else "Strong Bearish Alignment") if aligned else "Bullish Bias" if value >= 2 else "Bearish Bias" if value <= -2 else "Neutral / Mixed"
    return {"timeframes": trends, "score": value, "label": label, "aligned": aligned}
