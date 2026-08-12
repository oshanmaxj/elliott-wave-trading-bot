from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reasons: list[str]
    risk: Decimal | None
    risk_rewards: tuple[Decimal | None, Decimal | None, Decimal | None]


def directional_risk(direction: str, entry: Decimal, stop: Decimal) -> Decimal:
    return entry - stop if direction == "bullish" else stop - entry


def directional_reward(direction: str, entry: Decimal, target: Decimal) -> Decimal:
    return target - entry if direction == "bullish" else entry - target


def validate_geometry(
    direction: str,
    entry_min: Decimal | None,
    entry_max: Decimal | None,
    preferred_entry: Decimal | None,
    stop_loss: Decimal | None,
    targets: Iterable[Decimal | None],
    invalidation_price: Decimal | None = None,
    minimum_rr: Decimal = Decimal("0"),
    minimum_rr_target: int = 2,
) -> ValidationResult:
    reasons: list[str] = []
    targets_tuple = tuple(targets)
    if direction not in {"bullish", "bearish"}:
        reasons.append("invalid_direction")
    if entry_min is None or entry_max is None or entry_min > entry_max:
        reasons.append("invalid_entry_zone")
    if (
        preferred_entry is None
        or entry_min is None
        or entry_max is None
        or not entry_min <= preferred_entry <= entry_max
    ):
        reasons.append("entry_outside_zone")
    risk = None
    if preferred_entry is None or stop_loss is None:
        reasons.append("invalid_stop_side")
    else:
        risk = directional_risk(direction, preferred_entry, stop_loss)
        zone_stop_valid = (
            entry_min is not None
            and entry_max is not None
            and (
                (direction == "bullish" and stop_loss < entry_min)
                or (direction == "bearish" and stop_loss > entry_max)
            )
        )
        if risk <= 0 or not zone_stop_valid:
            reasons.append("invalid_stop_side")
        if risk <= 0:
            reasons.append("negative_risk")
    if invalidation_price is not None and preferred_entry is not None:
        invalidation_valid = (
            direction == "bullish" and invalidation_price < preferred_entry
        ) or (direction == "bearish" and invalidation_price > preferred_entry)
        if not invalidation_valid:
            reasons.append("invalid_invalidation_side")

    rrs: list[Decimal | None] = []
    previous = preferred_entry
    for target in targets_tuple:
        if target is None or preferred_entry is None or risk is None or risk <= 0:
            rrs.append(None)
            continue
        reward = directional_reward(direction, preferred_entry, target)
        ordered = previous is None or (
            (direction == "bullish" and target > previous)
            or (direction == "bearish" and target < previous)
        )
        if reward <= 0 or not ordered:
            reasons.append("invalid_target_side")
            rrs.append(None)
        else:
            rrs.append(reward / risk)
            previous = target
    while len(rrs) < 3:
        rrs.append(None)
    if not any(value is not None for value in rrs):
        reasons.append("invalid_target_side")
    index = minimum_rr_target - 1
    if minimum_rr > 0 and (index >= len(rrs) or rrs[index] is None or rrs[index] < minimum_rr):
        reasons.append("invalid_rr")
    unique_reasons = list(dict.fromkeys(reasons))
    return ValidationResult(
        not unique_reasons, unique_reasons, risk, tuple(rrs[:3])  # type: ignore[arg-type]
    )


def validate_setup(setup: Any, minimum_rr: Decimal = Decimal("1.5")) -> ValidationResult:
    return validate_geometry(
        setup.direction,
        setup.entry_min,
        setup.entry_max,
        setup.preferred_entry,
        setup.stop_loss,
        (setup.take_profit_1, setup.take_profit_2, setup.take_profit_3),
        getattr(setup, "invalidation_price", setup.stop_loss),
        minimum_rr,
    )
