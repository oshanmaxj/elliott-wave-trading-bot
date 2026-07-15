from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class WaveCandidate:
    direction: str
    pattern_type: str
    labels: tuple[str, ...]
    swings: tuple[Any, ...]
    valid: bool
    rules_passed: tuple[str, ...]
    rules_failed: tuple[str, ...]
    fibonacci: dict[str, Any]
    confidence_score: Decimal
    invalidation_price: Decimal
    target_min: Decimal | None
    target_max: Decimal | None
    phase: str


def _fib_score(
    actual: float, targets: tuple[float, ...], tolerance: float
) -> dict[str, float]:
    nearest = min(targets, key=lambda target: abs(actual - target))
    score = max(0.0, 1 - abs(actual - nearest) / max(nearest * tolerance, 1e-9))
    return {"actual": round(actual, 6), "nearest": nearest, "score": round(score, 6)}


def _chronological(points: list[Any]) -> bool:
    times = [point.candle.open_time for point in points]
    return all(left < right for left, right in zip(times, times[1:]))


def _alternates(points: list[Any], starts_low: bool) -> bool:
    expected = [
        "low" if (index % 2 == 0) == starts_low else "high"
        for index in range(len(points))
    ]
    return [point.swing_type for point in points] == expected


def _higher_timeframe_score(context: dict[str, Any], direction: str) -> int:
    value = context.get("higher_timeframe", False)
    if isinstance(value, bool):
        return 10 if value else 0
    if value in {"bullish", "bearish"}:
        return 10 if value == direction else -10
    return 0


def validate_impulse(
    points: list[Any],
    direction: str,
    tolerance: float = 0.15,
    context: dict[str, Any] | None = None,
) -> WaveCandidate:
    context = context or {}
    bullish = direction == "bullish"
    labels = tuple(str(index) for index in range(len(points)))
    passed, failed = [], []

    def rule(name: str, condition: bool) -> None:
        (passed if condition else failed).append(name)

    rule("strictly_chronological", _chronological(points))
    rule("confirmed_swing_alternation", _alternates(points, bullish))
    prices = [Decimal(point.price) for point in points]
    rule(
        "wave_2_does_not_exceed_origin",
        len(prices) < 3
        or (prices[2] > prices[0] if bullish else prices[2] < prices[0]),
    )
    if len(prices) >= 5:
        rule(
            "wave_4_no_wave_1_overlap",
            prices[4] > prices[1] if bullish else prices[4] < prices[1],
        )
    if len(prices) >= 6:
        lengths = [
            abs(prices[1] - prices[0]),
            abs(prices[3] - prices[2]),
            abs(prices[5] - prices[4]),
        ]
        rule("wave_3_not_shortest", lengths[1] >= min(lengths[0], lengths[2]))
        rule(
            "impulse_finishes_beyond_origin",
            prices[5] > prices[0] if bullish else prices[5] < prices[0],
        )
    fib: dict[str, Any] = {}
    wave1 = abs(prices[1] - prices[0]) if len(prices) > 1 else Decimal("0")
    if len(prices) >= 3 and wave1:
        fib["wave_2_retracement"] = _fib_score(
            float(abs(prices[1] - prices[2]) / wave1),
            (0.382, 0.5, 0.618, 0.786),
            tolerance,
        )
    if len(prices) >= 4 and wave1:
        fib["wave_3_extension"] = _fib_score(
            float(abs(prices[3] - prices[2]) / wave1), (1, 1.618, 2, 2.618), tolerance
        )
    if len(prices) >= 5 and prices[3] != prices[2]:
        fib["wave_4_retracement"] = _fib_score(
            float(abs(prices[3] - prices[4]) / abs(prices[3] - prices[2])),
            (0.236, 0.382, 0.5),
            tolerance,
        )
    if len(prices) >= 6 and wave1:
        fib["wave_5_vs_wave_1"] = _fib_score(
            float(abs(prices[5] - prices[4]) / wave1), (0.618, 1, 1.618), tolerance
        )
        one_through_three = abs(prices[3] - prices[0])
        if one_through_three:
            fib["wave_5_vs_waves_1_through_3"] = _fib_score(
                float(abs(prices[5] - prices[4]) / one_through_three),
                (0.618,),
                tolerance,
            )
    valid = not failed
    structural = 35 if valid else 0
    fib_points = (
        (sum(item["score"] for item in fib.values()) / len(fib) * 20) if fib else 0
    )
    support = (
        12 * context.get("structure", False)
        + _higher_timeframe_score(context, direction)
        + 8 * context.get("liquidity", False)
        + 7 * context.get("zone", False)
        + 5 * context.get("momentum", False)
        + 3
    )
    confidence = (
        Decimal(str(round(min(100, structural + fib_points + support), 2)))
        if valid
        else Decimal("0")
    )
    sign = Decimal("1") if bullish else Decimal("-1")
    anchor = prices[2] if len(prices) >= 3 else prices[-1]
    targets = (
        sorted(
            (
                anchor + sign * wave1 * Decimal("1.618"),
                anchor + sign * wave1 * Decimal("2.618"),
            )
        )
        if wave1
        else (None, None)
    )
    target_min, target_max = targets
    phase = "complete" if len(points) == 6 else f"projecting_wave_{len(points)}"
    return WaveCandidate(
        direction,
        f"{direction}_impulse",
        labels,
        tuple(points),
        valid,
        tuple(passed),
        tuple(failed),
        fib,
        confidence,
        prices[0],
        target_min,
        target_max,
        phase,
    )


def validate_zigzag(
    points: list[Any],
    direction: str,
    tolerance: float = 0.15,
    allow_truncation: bool = False,
    context: dict[str, Any] | None = None,
) -> WaveCandidate:
    context = context or {}
    bullish = direction == "bullish"
    labels = ("0", "A", "B", "C")[: len(points)]
    passed, failed = [], []

    def rule(name: str, condition: bool) -> None:
        (passed if condition else failed).append(name)

    rule("strictly_chronological", _chronological(points))
    rule("abc_alternation", _alternates(points, bullish))
    prices = [Decimal(point.price) for point in points]
    rule(
        "wave_b_does_not_invalidate_origin",
        len(prices) < 3
        or (prices[2] > prices[0] if bullish else prices[2] < prices[0]),
    )
    if len(prices) >= 4:
        beyond = prices[3] > prices[1] if bullish else prices[3] < prices[1]
        rule("wave_c_exceeds_wave_a_or_truncation_allowed", beyond or allow_truncation)
    a = abs(prices[1] - prices[0]) if len(prices) > 1 else Decimal("0")
    fib = {}
    if len(prices) >= 3 and a:
        fib["wave_b_retracement"] = _fib_score(
            float(abs(prices[1] - prices[2]) / a), (0.382, 0.5, 0.618, 0.786), tolerance
        )
    if len(prices) >= 4 and a:
        fib["wave_c_extension"] = _fib_score(
            float(abs(prices[3] - prices[2]) / a), (1, 1.618), tolerance
        )
    valid = not failed
    fib_points = (sum(x["score"] for x in fib.values()) / len(fib) * 20) if fib else 0
    support = (
        12 * context.get("structure", False)
        + _higher_timeframe_score(context, direction)
        + 8 * context.get("liquidity", False)
        + 7 * context.get("zone", False)
        + 5 * context.get("momentum", False)
        + 3
    )
    score = (
        Decimal(str(round(min(100, (35 if valid else 0) + fib_points + support), 2)))
        if valid
        else Decimal("0")
    )
    sign = Decimal("1") if bullish else Decimal("-1")
    anchor = prices[2] if len(prices) >= 3 else prices[-1]
    targets = sorted((anchor + sign * a, anchor + sign * a * Decimal("1.618")))
    return WaveCandidate(
        direction,
        f"{direction}_zigzag",
        labels,
        tuple(points),
        valid,
        tuple(passed),
        tuple(failed),
        fib,
        score,
        prices[0],
        targets[0],
        targets[1],
        "complete" if len(points) == 4 else "projecting_wave_c",
    )


def assign_degree(timeframe: str, points: list[Any], atr: float | None = None) -> str:
    base = {"15m": 0, "1h": 1, "4h": 2}.get(timeframe, 0)
    strength = sum(float(point.strength) for point in points) / max(len(points), 1)
    movement = (
        abs(float(points[-1].price - points[0].price))
        / max(abs(float(points[0].price)), 1e-9)
        * 100
    )
    duration = (
        points[-1].candle.open_time - points[0].candle.open_time
    ).total_seconds() / 3600
    atr_movement = abs(float(points[-1].price - points[0].price)) / atr if atr else 0
    if strength >= 0.8 and (movement >= 3 or atr_movement >= 5 or duration >= 48):
        base = min(2, base + 1)
    if strength < 0.25 and movement < 0.5:
        base = max(0, base - 1)
    return ("minor", "intermediate", "primary")[base]


def generate_candidates(
    swings: list[Any],
    tolerance: float = 0.15,
    allow_truncation: bool = False,
    context: dict[str, Any] | None = None,
) -> list[WaveCandidate]:
    results = []
    for size in (3, 5, 6):
        for start in range(max(0, len(swings) - 20), len(swings) - size + 1):
            window = swings[start : start + size]
            direction = "bullish" if window[0].swing_type == "low" else "bearish"
            candidate = validate_impulse(window, direction, tolerance, context)
            if candidate.valid:
                results.append(candidate)
    for size in (3, 4):
        for start in range(max(0, len(swings) - 20), len(swings) - size + 1):
            window = swings[start : start + size]
            direction = "bullish" if window[0].swing_type == "low" else "bearish"
            candidate = validate_zigzag(
                window, direction, tolerance, allow_truncation, context
            )
            if candidate.valid:
                results.append(candidate)
    return sorted(
        results,
        key=lambda item: (item.confidence_score, item.swings[-1].candle.open_time),
        reverse=True,
    )
