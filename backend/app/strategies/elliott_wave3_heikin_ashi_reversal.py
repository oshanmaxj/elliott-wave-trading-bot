"""Research-only Wave-3 + HA strategy gates, scoring and deterministic exits."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

D = Decimal
STRATEGY = "elliott_wave3_heikin_ashi_reversal"
LIVE_AUTO_EXECUTION_ENABLED = False
VARIANTS = ("A", "B")


@dataclass(frozen=True)
class ResearchParameters:
    ha_pullback_min_candles: int = 2
    ha_wick_body_max_ratio: Decimal = D("0.25")
    ha_body_atr_min_ratio: Decimal = D("0.25")
    ha_confirmation_required: bool = True
    research_score_threshold: int = 70
    stop_loss_atr_buffer: Decimal = D("0.25")
    max_stop_atr_ratio: Decimal = D("3")


def wave3_gate(count: Any, decision_time: Any, real_price: Decimal) -> tuple[bool, list[int]]:
    """Require an as-of valid impulse with persisted Wave 0/1/2 points."""
    if not count or count.timeframe != "15m" or count.detected_at > decision_time or count.invalidated_at:
        return False, []
    points = sorted(count.points, key=lambda p: p.sequence_number)
    ids = [p.id for p in points]
    current = str((count.metadata_json or {}).get("current_wave", ""))
    impulse = "impulse" in count.pattern_type and count.direction in {"bullish", "bearish"}
    phase_ok = current in {"2", "3"} or (len(points) == 3 and "projecting_wave_3" in str((count.metadata_json or {}).get("phase", "")))
    intact = real_price > D(count.invalidation_price) if count.direction == "bullish" else real_price < D(count.invalidation_price)
    return bool(impulse and len(points) >= 3 and phase_ok and intact), ids


def score_components(*, bos=False, choch_bos=False, ha_reversal=False, fvg=False,
                     order_block=False, sweep=False, volume=False, alignment=False) -> dict[str, int]:
    return {
        "5m_bos": 20 if bos else 0, "5m_choch_to_bos": 15 if choch_bos else 0,
        "1m_confirmed_ha_reversal": 20 if ha_reversal else 0,
        "fvg_alignment": 10 if fvg else 0, "order_block_alignment": 10 if order_block else 0,
        "liquidity_sweep": 10 if sweep else 0, "volume_expansion": 5 if volume else 0,
        "15m_directional_alignment": 10 if alignment else 0,
    }


def total_score(components: dict[str, int]) -> int:
    return sum(components.values())


def structural_stop(direction: str, wave2_price: Decimal, swing_price: Decimal | None,
                    atr: Decimal, buffer_ratio: Decimal) -> Decimal:
    anchor = min(wave2_price, swing_price) if direction == "bullish" and swing_price is not None else max(wave2_price, swing_price) if swing_price is not None else wave2_price
    return anchor - atr * buffer_ratio if direction == "bullish" else anchor + atr * buffer_ratio


def exit_priority(*, hard_stop=False, invalidated=False, opposite_ha=False) -> str | None:
    if hard_stop:
        return "hard_stop"
    if invalidated:
        return "elliott_invalidation"
    if opposite_ha:
        return "5m_opposite_ha_reversal"
    return None


def variant_b_fractions(reached_1r: bool, reached_2r: bool) -> tuple[Decimal, Decimal]:
    """Return realized and runner fractions; targets are independently one-shot."""
    realized = (D("0.25") if reached_1r else D("0")) + (D("0.25") if reached_2r else D("0"))
    return realized, D("1") - realized
