from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.core.constants import TIMEFRAME_MS


@dataclass(frozen=True)
class SetupDecision:
    direction: str
    strategy: str
    status: str
    fvg: Any | None
    order_block: Any | None
    entry_min: Decimal | None
    entry_max: Decimal | None
    preferred_entry: Decimal | None
    stop_loss: Decimal | None
    targets: tuple[Decimal | None, Decimal | None, Decimal | None]
    risk_rewards: tuple[Decimal | None, Decimal | None, Decimal | None]
    confidence_score: Decimal
    score_breakdown: dict[str, float]
    conditions: dict[str, Any]
    rejection_reasons: list[str]
    expires_at: Any


def _zone(direction: str, fvgs: list[Any], blocks: list[Any]) -> tuple[Any | None, Any | None, Decimal | None, Decimal | None]:
    fvg = next((z for z in reversed(fvgs) if z.direction == direction and z.status in {"active", "partially_mitigated"}), None)
    block = next((z for z in reversed(blocks) if z.direction == direction and z.status in {"active", "partially_mitigated"}), None)
    if fvg and block:
        low, high = max(Decimal(fvg.lower_price), Decimal(block.bottom_price)), min(Decimal(fvg.upper_price), Decimal(block.top_price))
        if low < high:
            return fvg, block, low, high
    if fvg:
        return fvg, block, Decimal(fvg.lower_price), Decimal(fvg.upper_price)
    if block:
        return fvg, block, Decimal(block.bottom_price), Decimal(block.top_price)
    return None, None, None, None


def generate_setup(direction: str, structure_event: Any, candle: Any, settings: Any, fvgs: list[Any], blocks: list[Any], swings: list[Any], pools: list[Any], indicators: dict[str, Any], htf_trend: str, premium_zone: dict[str, Any] | None, sweep: Any | None = None, continuation: bool = False) -> SetupDecision:
    strategy = f"{direction}_{'continuation' if continuation else 'liquidity_reversal'}"
    fvg, block, entry_min, entry_max = _zone(direction, fvgs, blocks)
    reasons: list[str] = []
    if not continuation and (not sweep or sweep.status != "confirmed"):
        reasons.append("expired or unconfirmed liquidity sweep")
    if structure_event.direction != direction:
        reasons.append("hard structure invalidation")
    if entry_min is None or entry_max is None:
        reasons.append("no valid entry zone")
    preferred = None if entry_min is None else (entry_min + entry_max) / 2
    atr = Decimal(str(indicators.get("atr14") or 0))
    fallback_buffer = (preferred or Decimal(candle.close)) * Decimal("0.001")
    buffer = max(fallback_buffer, atr * Decimal(str(settings.stop_loss_atr_buffer)))
    extreme = Decimal(sweep.extreme_price) if sweep else Decimal(candle.low if direction == "bullish" else candle.high)
    stop = (extreme - buffer) if direction == "bullish" else (extreme + buffer)
    if preferred is not None and ((direction == "bullish" and stop >= preferred) or (direction == "bearish" and stop <= preferred)):
        reasons.append("stop loss on the wrong side")
    risk = abs(preferred - stop) if preferred is not None else Decimal("0")
    sign = Decimal("1") if direction == "bullish" else Decimal("-1")
    fallback = tuple((preferred + sign * risk * multiple) if preferred is not None else None for multiple in (1, 2, 3))
    target_swings = [Decimal(s.price) for s in swings if s.swing_type == ("high" if direction == "bullish" else "low") and preferred is not None and ((direction == "bullish" and s.price > preferred) or (direction == "bearish" and s.price < preferred))]
    target_pools = [Decimal(p.price) for p in pools if p.type == ("BSL" if direction == "bullish" else "SSL") and getattr(p, "status", "active") == "active" and preferred is not None and ((direction == "bullish" and p.price > preferred) or (direction == "bearish" and p.price < preferred))]
    tp1 = (min(target_swings) if direction == "bullish" else max(target_swings)) if target_swings else fallback[0]
    if tp1 is not None and fallback[0] is not None:
        tp1 = max(tp1, fallback[0]) if direction == "bullish" else min(tp1, fallback[0])
    tp2 = (min(target_pools) if direction == "bullish" else max(target_pools)) if target_pools else fallback[1]
    if tp2 is not None and fallback[1] is not None:
        tp2 = max(tp2, fallback[1]) if direction == "bullish" else min(tp2, fallback[1])
    tp3 = fallback[2]
    targets = (tp1, tp2, tp3)
    rrs = tuple(None if risk <= 0 or target is None or preferred is None else abs(target - preferred) / risk for target in targets)
    if rrs[1] is None or rrs[1] < Decimal(str(settings.minimum_reward_to_risk)):
        reasons.append("reward-to-risk below threshold")
    if tp2 is None:
        reasons.append("no target liquidity or valid fallback target")
    counter_trend = htf_trend in ({"bearish"} if direction == "bullish" else {"bullish"})
    if counter_trend and not settings.counter_trend_setups_enabled:
        reasons.append("higher-timeframe counter-trend setup disabled")
    ema20, ema50 = indicators.get("ema20"), indicators.get("ema50")
    ema_aligned = ema20 is not None and ema50 is not None and ((direction == "bullish" and ema20 >= ema50) or (direction == "bearish" and ema20 <= ema50))
    in_discount = bool(premium_zone and preferred is not None and ((direction == "bullish" and preferred <= Decimal(premium_zone["equilibrium"])) or (direction == "bearish" and preferred >= Decimal(premium_zone["equilibrium"]))))
    breakdown = {"liquidity_sweep_quality": float(sweep.confidence_score) / 100 * 22 if sweep else 0, "structure_confirmation": 18, "fvg_quality": 12 if fvg else 0, "order_block_quality": 12 if block else 0, "multi_timeframe_alignment": 12 if not counter_trend else 0, "premium_discount_location": 8 if in_discount else 0, "ema_momentum_alignment": 6 if ema_aligned else 0, "target_liquidity_quality": 5 if target_pools else 3, "data_freshness": 5}
    score = Decimal(str(round(min(100, sum(breakdown.values())), 2)))
    required = settings.counter_trend_minimum_confidence if counter_trend else settings.minimum_setup_confidence
    if score < Decimal(str(required)):
        reasons.append("confidence below threshold")
    status = "rejected" if reasons else "ready" if score >= 70 else "watching"
    expires = candle.close_time + timedelta(milliseconds=TIMEFRAME_MS[candle.timeframe] * settings.setup_expiry_candles)
    return SetupDecision(direction, strategy, status, fvg, block, entry_min, entry_max, preferred, stop if preferred is not None else None, targets, rrs, score, breakdown, {"counter_trend": counter_trend, "structure_event": structure_event.event_type, "zone_overlap": bool(fvg and block)}, reasons, expires)


def update_setup_lifecycle(setup: Any, candle: Any) -> str | None:
    if setup.status not in {"watching", "ready", "triggered"}:
        return None
    if candle.close_time > setup.expires_at and setup.status != "triggered":
        return "expired"
    if setup.invalidation_price is not None and ((setup.direction == "bullish" and candle.low <= setup.invalidation_price) or (setup.direction == "bearish" and candle.high >= setup.invalidation_price)):
        return "invalidated"
    if setup.status in {"watching", "ready"} and setup.entry_min is not None and candle.high >= setup.entry_min and candle.low <= setup.entry_max:
        return "triggered"
    return None
