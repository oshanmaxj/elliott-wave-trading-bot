from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SweepDecision:
    direction: str
    sweep_type: str
    status: str
    extreme_price: Decimal
    reclaimed_price: Decimal | None
    penetration_percentage: Decimal
    rejection_strength: Decimal
    confidence_score: Decimal
    score_breakdown: dict[str, float]


def _metrics(pool: Any, candle: Any) -> tuple[str, Decimal, Decimal, Decimal, bool]:
    price = Decimal(pool.price)
    bullish = pool.type in {"SSL", "EQL"}
    direction = "bullish" if bullish else "bearish"
    extreme = Decimal(candle.low if bullish else candle.high)
    penetration = max(Decimal("0"), (price - extreme if bullish else extreme - price) / price * 100)
    candle_range = Decimal(candle.high) - Decimal(candle.low)
    wick = (Decimal(candle.close) - Decimal(candle.low)) if bullish else (Decimal(candle.high) - Decimal(candle.close))
    wick_ratio = Decimal("0") if candle_range <= 0 else max(Decimal("0"), min(Decimal("1"), wick / candle_range))
    reclaimed = Decimal(candle.close) > price if bullish else Decimal(candle.close) < price
    return direction, extreme, penetration, wick_ratio, reclaimed


def sweep_confidence(pool: Any, penetration: Decimal, reclaimed: bool, wick_ratio: Decimal, volume_ratio: float | None, settings: Any, htf_aligned: bool = False, structure_support: bool = False) -> tuple[Decimal, dict[str, float]]:
    minimum, maximum = settings.sweep_minimum_penetration_percentage, settings.sweep_maximum_penetration_percentage
    span = max(maximum - minimum, 1e-9)
    penetration_quality = max(0.0, 1 - abs(float(penetration) - (minimum + maximum) / 2) / span * 2)
    breakdown = {
        "liquidity_pool_strength": min(20.0, float(pool.strength) * 20),
        "penetration_quality": penetration_quality * 15,
        "reclaim_quality": 20.0 if reclaimed else 0.0,
        "rejection_wick": min(15.0, float(wick_ratio) / max(settings.sweep_minimum_wick_ratio, 1e-9) * 15),
        "volume_confirmation": 10.0 if volume_ratio is not None and (settings.sweep_minimum_volume_ratio is None or volume_ratio >= settings.sweep_minimum_volume_ratio) else 5.0 if settings.sweep_minimum_volume_ratio is None else 0.0,
        "higher_timeframe_alignment": 10.0 if htf_aligned else 0.0,
        "structure_confirmation": 10.0 if structure_support else 0.0,
    }
    return Decimal(str(round(min(100.0, sum(breakdown.values())), 2))), breakdown


def detect_sweep(pool: Any, candle: Any, settings: Any, volume_ratio: float | None = None, htf_aligned: bool = False, structure_support: bool = False) -> SweepDecision | None:
    if getattr(pool, "status", "active") != "active" or float(pool.strength) < settings.sweep_liquidity_strength_threshold:
        return None
    direction, extreme, penetration, wick_ratio, reclaimed = _metrics(pool, candle)
    if penetration < Decimal(str(settings.sweep_minimum_penetration_percentage)):
        return None
    hard_invalid = penetration > Decimal(str(settings.sweep_maximum_penetration_percentage))
    same_candle = reclaimed and settings.sweep_allow_same_candle_confirmation and (not settings.sweep_require_closed_confirmation or candle.is_closed)
    score, breakdown = sweep_confidence(pool, penetration, same_candle, wick_ratio, volume_ratio, settings, htf_aligned, structure_support)
    sweep_type = "sell_side_sweep" if direction == "bullish" else "buy_side_sweep"
    if hard_invalid:
        return SweepDecision(direction, "failed_sweep", "invalidated", extreme, None, penetration, wick_ratio, Decimal("0"), {**breakdown, "hard_invalidation": 1})
    if score < Decimal(str(settings.minimum_sweep_confidence)):
        return None
    status = "confirmed" if same_candle and score >= Decimal("70") else "candidate"
    return SweepDecision(direction, sweep_type, status, extreme, Decimal(candle.close) if same_candle else None, penetration, wick_ratio, score, breakdown)


def update_sweep(candidate: Any, pool: Any, candle: Any, age_candles: int, settings: Any, volume_ratio: float | None = None, htf_aligned: bool = False, structure_support: bool = False) -> SweepDecision | None:
    if candidate.status != "candidate":
        return None
    direction, extreme, penetration, wick_ratio, reclaimed = _metrics(pool, candle)
    combined_extreme = min(Decimal(candidate.extreme_price), extreme) if direction == "bullish" else max(Decimal(candidate.extreme_price), extreme)
    total_penetration = max(Decimal(candidate.penetration_percentage), penetration)
    if total_penetration > Decimal(str(settings.sweep_maximum_penetration_percentage)) or age_candles > settings.sweep_confirmation_candles:
        return SweepDecision(direction, "failed_sweep", "invalidated", combined_extreme, None, total_penetration, wick_ratio, Decimal("0"), {"hard_invalidation": 1})
    if age_candles > settings.sweep_expiry_candles:
        return SweepDecision(direction, candidate.sweep_type, "expired", combined_extreme, None, total_penetration, wick_ratio, Decimal(candidate.confidence_score), candidate.metadata_json.get("score_breakdown", {}))
    if not reclaimed:
        return None
    score, breakdown = sweep_confidence(pool, total_penetration, True, wick_ratio, volume_ratio, settings, htf_aligned, structure_support)
    status = "confirmed" if score >= Decimal("70") else "candidate"
    return SweepDecision(direction, candidate.sweep_type, status, combined_extreme, Decimal(candle.close), total_penetration, wick_ratio, score, breakdown)
