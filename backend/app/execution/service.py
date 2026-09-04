from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from sqlalchemy import func, select
from app.execution.filters import (
    floor_quantity_to_step,
    quantity_limits,
    validate_notional,
    validate_symbol_tradeability,
)
from app.execution.runtime import runtime_state
from app.models import ExecutionOrder, LivePosition
from app.execution.reconciliation import ACTIVE_POSITION_STATUSES
from app.trading.validation import validate_setup

# Fallback used when a risk_config_json value for minimum_rr is absent or
# unparsable; matches the previous RuntimeSettings.minimum_reward_to_risk default.
DEFAULT_MINIMUM_RR = Decimal("1.5")
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = Decimal("20")
# Mirror app.core.config.Settings' own field defaults, used whenever the
# settings object passed to risk_config() doesn't define an attribute (e.g. a
# lightweight test double) rather than raising AttributeError.
DEFAULT_MAX_RISK_PER_TRADE_PCT = Decimal("0.25")
DEFAULT_MAX_DAILY_LOSS_PCT = Decimal("1.0")
DEFAULT_MAX_OPEN_POSITIONS = 1
DEFAULT_MAX_SYMBOL_EXPOSURE_PCT = Decimal("10")
DEFAULT_MIN_EXECUTION_CONFIDENCE = Decimal("75")


def setup_fingerprint(symbol: str, setup, window_minutes: int = 15) -> str:
    detected = setup.detected_at
    if detected.tzinfo is None:
        detected = detected.replace(tzinfo=timezone.utc)
    bucket = int(detected.timestamp()) // (window_minutes * 60)
    identity = f"{symbol.upper()}|{setup.strategy}|{setup.direction}|{bucket}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _parse_decimal(raw: dict, key: str, default: Decimal) -> Decimal:
    value = raw.get(key)
    if value in (None, ""):
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _parse_positive_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key)
    if value in (None, ""):
        return default
    try:
        parsed = int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def risk_config(db, settings) -> dict:
    """Merge BotRuntimeState.risk_config_json onto .env Settings defaults.

    Every field is parsed defensively: a missing key, an empty string, or a
    value that fails to parse falls back to the existing .env-driven default
    rather than raising, so a bad admin-entered string can never crash
    execution.
    """
    runtime = runtime_state(db)
    raw = (runtime.risk_config_json if runtime else {}) or {}
    return {
        "risk_per_trade_pct": _parse_decimal(
            raw,
            "risk_per_trade_pct",
            getattr(settings, "max_risk_per_trade_pct", DEFAULT_MAX_RISK_PER_TRADE_PCT),
        ),
        "daily_loss_pct": _parse_decimal(
            raw,
            "daily_loss_pct",
            getattr(settings, "max_daily_loss_pct", DEFAULT_MAX_DAILY_LOSS_PCT),
        ),
        "max_open_positions": _parse_positive_int(
            raw,
            "max_open_positions",
            getattr(settings, "max_open_positions", DEFAULT_MAX_OPEN_POSITIONS),
        ),
        "max_symbol_exposure_pct": _parse_decimal(
            raw,
            "max_symbol_exposure_pct",
            getattr(settings, "max_symbol_exposure_pct", DEFAULT_MAX_SYMBOL_EXPOSURE_PCT),
        ),
        "max_total_exposure_pct": _parse_decimal(
            raw, "max_total_exposure_pct", DEFAULT_MAX_TOTAL_EXPOSURE_PCT
        ),
        "minimum_confidence": _parse_decimal(
            raw,
            "minimum_confidence",
            getattr(settings, "min_execution_confidence", DEFAULT_MIN_EXECUTION_CONFIDENCE),
        ),
        "minimum_rr": _parse_decimal(raw, "minimum_rr", DEFAULT_MINIMUM_RR),
    }


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
        cfg = risk_config(db, self.s)
        if not self.s.binance_execution_enabled:
            reasons.append("execution_disabled")
        if self.s.execution_mode == "disabled":
            reasons.append("execution_mode_disabled")
        if kill_switch:
            reasons.append("kill_switch_enabled")
        if symbol.symbol not in self.s.allowed_execution_symbols:
            reasons.append("symbol_not_allowed")
        if setup.strategy not in self.s.allowed_execution_strategies:
            reasons.append("execution_strategy_not_allowed")
        runtime = runtime_state(db)
        enabled_timeframes = (
            runtime.enabled_timeframes_json if runtime else ["15m", "1h", "4h"]
        )
        if setup.setup_timeframe not in enabled_timeframes:
            reasons.append("timeframe_not_enabled")
        if setup.status not in {"ready", "eligible", "approved", "pending_approval", "triggered"}:
            reasons.append("setup_not_eligible")
        if setup.confidence_score < cfg["minimum_confidence"]:
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
        minimum_rr = cfg["minimum_rr"]
        geometry = validate_setup(setup, minimum_rr)
        if not geometry.valid:
            reasons.extend(reason for reason in geometry.reasons if reason not in reasons)
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
        risk = equity * cfg["risk_per_trade_pct"] / Decimal("100")
        distance = abs(entry - stop)
        calculated = risk / distance if distance else zero
        minimum, maximum, step = quantity_limits(symbol_info)
        exposure_cap = equity * cfg["max_symbol_exposure_pct"] / Decimal("100")
        if step <= 0:
            reasons.append("invalid_quantity_step")
            adjusted = zero
        else:
            adjusted = (
                floor_quantity_to_step(min(calculated, maximum), step)
                if calculated
                else zero
            )
            adjusted = min(
                adjusted,
                floor_quantity_to_step(exposure_cap / market_price, step)
                if market_price
                else zero,
            )
            adjusted = min(
                adjusted,
                floor_quantity_to_step(equity / market_price, step)
                if market_price
                else zero,
            )
        notional = adjusted * market_price
        if adjusted < minimum:
            reasons.append("quantity_below_minimum")
        reasons += validate_notional(market_price, adjusted, symbol_info)
        if notional > equity:
            reasons.append("insufficient_balance")
        if (
            db.scalar(
                select(func.count())
                .select_from(LivePosition)
                .where(LivePosition.status.in_(ACTIVE_POSITION_STATUSES))
            )
            >= cfg["max_open_positions"]
        ):
            reasons.append("max_open_positions_reached")
        # weekly_loss_pct and max_drawdown_pct are stored in risk_config_json but
        # intentionally left unenforced: there is no weekly risk ledger or a live
        # equity high-water-mark tracked anywhere in this codebase to check them
        # against (DailyRiskLedger is daily-only; PaperAccount.drawdown_pct belongs
        # to the unrelated paper simulator).
        existing_notional = db.scalar(
            select(func.sum(LivePosition.remaining_quantity * LivePosition.average_entry))
            .where(LivePosition.status.in_(ACTIVE_POSITION_STATUSES))
        ) or zero
        total_notional = Decimal(existing_notional) + notional
        total_exposure_cap = equity * cfg["max_total_exposure_pct"] / Decimal("100")
        if total_notional > total_exposure_cap:
            reasons.append("max_total_exposure_reached")
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
