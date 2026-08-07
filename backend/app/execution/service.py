from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from sqlalchemy import func, select
from app.execution.filters import (
    floor_quantity_to_step,
    quantity_limits,
    validate_notional,
    validate_symbol_tradeability,
)
from app.models import BotRuntimeState, ExecutionOrder, LivePosition


def setup_fingerprint(symbol: str, setup, window_minutes: int = 15) -> str:
    detected = setup.detected_at
    if detected.tzinfo is None:
        detected = detected.replace(tzinfo=timezone.utc)
    bucket = int(detected.timestamp()) // (window_minutes * 60)
    identity = f"{symbol.upper()}|{setup.strategy}|{setup.direction}|{bucket}"
    return hashlib.sha256(identity.encode()).hexdigest()


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str]
    risk_amount: Decimal
    calculated_quantity: Decimal
    adjusted_quantity: Decimal
    estimated_notional: Decimal
    estimated_fee: Decimal
    estimated_max_loss: Decimal
    entry: Decimal
    stop: Decimal
    targets: list[Decimal]

    def json(self):
        return {
            k: (
                str(v)
                if isinstance(v, Decimal)
                else [str(x) for x in v]
                if k == "targets"
                else v
            )
            for k, v in asdict(self).items()
        }


class ExecutionRiskEngine:
    def __init__(self, settings):
        self.s = settings

    def evaluate(
        self,
        db,
        setup,
        symbol,
        account,
        market_price: Decimal,
        symbol_info: dict,
        *,
        kill_switch=False,
    ):
        reasons = []
        zero = Decimal("0")
        entry = setup.preferred_entry or zero
        stop = setup.stop_loss or zero
        if not self.s.binance_execution_enabled:
            reasons.append("execution_disabled")
        if self.s.execution_mode == "disabled":
            reasons.append("execution_mode_disabled")
        if kill_switch:
            reasons.append("kill_switch_enabled")
        if symbol.symbol not in self.s.allowed_execution_symbols:
            reasons.append("symbol_not_allowed")
        if setup.strategy not in self.s.allowed_execution_strategies:
            reasons.append("strategy_not_allowed")
        runtime = db.scalar(select(BotRuntimeState).limit(1))
        enabled_timeframes = (
            runtime.enabled_timeframes_json if runtime else ["15m", "1h", "4h"]
        )
        if setup.setup_timeframe not in enabled_timeframes:
            reasons.append("timeframe_not_enabled")
        if setup.status not in {"ready", "eligible", "approved", "pending_approval"}:
            reasons.append("setup_not_eligible")
        if setup.confidence_score < self.s.min_execution_confidence:
            reasons.append("confidence_below_minimum")
        now = datetime.now(timezone.utc)
        expires = (
            setup.expires_at
            if setup.expires_at.tzinfo
            else setup.expires_at.replace(tzinfo=timezone.utc)
        )
        if expires <= now:
            reasons.append("setup_expired")
        if db.scalar(
            select(ExecutionOrder.id)
            .where(ExecutionOrder.trade_setup_id == setup.id)
            .limit(1)
        ):
            reasons.append("setup_already_executed")
        fingerprint = setup_fingerprint(symbol.symbol, setup)
        if db.scalar(
            select(ExecutionOrder.id)
            .where(ExecutionOrder.setup_fingerprint == fingerprint)
            .limit(1)
        ):
            reasons.append("duplicate_setup_window")
        if not entry or not stop or entry == stop:
            reasons.append("invalid_stop_distance")
        if (
            setup.entry_min is None
            or setup.entry_max is None
            or not (setup.entry_min <= market_price <= setup.entry_max)
        ):
            reasons.append("market_outside_entry_zone")
        bullish = setup.direction.lower() in {"bullish", "long", "buy"}
        if bullish and not (
            stop < entry
            and all(
                t is None or t > entry
                for t in [setup.take_profit_1, setup.take_profit_2, setup.take_profit_3]
            )
        ):
            reasons.append("invalid_trade_geometry")
        if not bullish:
            reasons.append("unsupported_for_spot")
        reasons += validate_symbol_tradeability(symbol_info)
        balances = {x["asset"]: Decimal(x["free"]) for x in account.get("balances", [])}
        equity = balances.get(symbol.quote_asset, zero)
        risk = equity * self.s.max_risk_per_trade_pct / Decimal("100")
        distance = abs(entry - stop)
        calculated = risk / distance if distance else zero
        minimum, maximum, step = quantity_limits(symbol_info)
        adjusted = (
            floor_quantity_to_step(min(calculated, maximum), step)
            if calculated
            else zero
        )
        exposure_cap = equity * self.s.max_symbol_exposure_pct / Decimal("100")
        adjusted = min(
            adjusted,
            floor_quantity_to_step(exposure_cap / entry, step) if entry else zero,
        )
        adjusted = min(
            adjusted, floor_quantity_to_step(equity / entry, step) if entry else zero
        )
        notional = adjusted * entry
        if adjusted < minimum:
            reasons.append("quantity_below_minimum")
        reasons += validate_notional(entry, adjusted, symbol_info)
        if notional > equity:
            reasons.append("insufficient_balance")
        if (
            db.scalar(
                select(func.count())
                .select_from(LivePosition)
                .where(LivePosition.status.in_(["open", "partially_closed"]))
            )
            >= self.s.max_open_positions
        ):
            reasons.append("max_open_positions_reached")
        return RiskDecision(
            not reasons,
            reasons,
            risk,
            calculated,
            adjusted,
            notional,
            notional * Decimal("0.001"),
            adjusted * distance,
            entry,
            stop,
            [
                x
                for x in [setup.take_profit_1, setup.take_profit_2, setup.take_profit_3]
                if x is not None
            ],
        )


def client_order_id(environment: str, setup_id: int, role="entry", attempt=1):
    return f"ws-{environment[:4]}-{setup_id}-{role}-{attempt}"
