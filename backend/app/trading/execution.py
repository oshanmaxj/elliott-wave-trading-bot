from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def position_size(equity: Decimal, risk_pct: Decimal, entry: Decimal, stop: Decimal, max_risk_pct: Decimal = Decimal("1")) -> tuple[Decimal, Decimal]:
    applied_pct = min(risk_pct, max_risk_pct)
    risk_amount = equity * applied_pct / Decimal("100")
    distance = abs(entry - stop)
    if equity <= 0 or risk_amount <= 0 or distance <= 0:
        raise ValueError("invalid position sizing inputs")
    return risk_amount, risk_amount / distance


def slipped_price(price: Decimal, direction: str, bps: Decimal, entering: bool) -> Decimal:
    adverse_sign = Decimal("1") if (direction == "bullish") == entering else Decimal("-1")
    return price * (Decimal("1") + adverse_sign * bps / Decimal("10000"))


def execution_fee(price: Decimal, quantity: Decimal, fee_pct: Decimal) -> Decimal:
    return price * quantity * fee_pct / Decimal("100")


@dataclass(frozen=True)
class CandleEvent:
    reason: str
    price: Decimal | None
    ambiguous: bool = False


def candle_exit(direction: str, high: Decimal, low: Decimal, stop: Decimal, target: Decimal, policy: str = "stop_first") -> CandleEvent:
    stop_hit = low <= stop if direction == "bullish" else high >= stop
    target_hit = high >= target if direction == "bullish" else low <= target
    if stop_hit and target_hit:
        if policy == "skip_ambiguous":
            return CandleEvent("ambiguous_skipped", None, True)
        if policy == "target_first":
            return CandleEvent("take_profit", target, True)
        return CandleEvent("stop_loss", stop, True)
    if stop_hit:
        return CandleEvent("stop_loss", stop)
    if target_hit:
        return CandleEvent("take_profit", target)
    return CandleEvent("open", None)


def pnl(direction: str, entry: Decimal, exit_price: Decimal, quantity: Decimal, fees: Decimal = Decimal("0")) -> Decimal:
    gross = (exit_price - entry) * quantity if direction == "bullish" else (entry - exit_price) * quantity
    return gross - fees


def risk_guards(
    equity: Decimal,
    max_equity: Decimal,
    daily_pnl: Decimal,
    starting_day_equity: Decimal,
    open_positions: int,
    max_open_positions: int,
    max_daily_loss_pct: Decimal,
    max_drawdown_pause_pct: Decimal,
) -> list[str]:
    reasons = []
    if open_positions >= max_open_positions:
        reasons.append("max_open_positions")
    if starting_day_equity > 0 and daily_pnl < 0 and abs(daily_pnl) / starting_day_equity * 100 >= max_daily_loss_pct:
        reasons.append("daily_loss_guard")
    if max_equity > 0 and (max_equity - equity) / max_equity * 100 >= max_drawdown_pause_pct:
        reasons.append("drawdown_guard")
    return reasons


def setup_available(setup: Any, candle: Any) -> bool:
    """No-look-ahead boundary: setup and every supporting fact must exist by candle close."""
    if setup.detected_at > candle.close_time:
        return False
    return not setup.expires_at < candle.open_time
