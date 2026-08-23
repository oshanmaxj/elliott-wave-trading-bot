"""Idempotent forward testing against persisted Binance production Spot candles only."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Candle, LivePosition, PaperForwardTrade, Symbol, TradeSetup
from app.trading.execution import candle_exit, execution_fee, pnl
from app.trading.validation import validate_setup

D = Decimal
SOURCE = "binance_production_spot_db"
TERMINAL = {"closed", "expired", "invalidated"}
TP_FRACTIONS = {1: D("0.30"), 2: D("0.40"), 3: D("0.30")}


def setup_is_eligible(setup: TradeSetup) -> bool:
    required = (setup.preferred_entry, setup.entry_min, setup.entry_max, setup.stop_loss)
    return (
        not setup.rejection_reasons_json
        and all(value is not None for value in required)
        and any(value is not None for value in (setup.take_profit_1, setup.take_profit_2, setup.take_profit_3))
        and validate_setup(setup).valid
    )


def enroll_setup(db: Session, setup: TradeSetup, fee_rate_pct: Decimal = D("0.1")) -> PaperForwardTrade | None:
    existing = db.scalar(select(PaperForwardTrade).where(PaperForwardTrade.setup_id == setup.id))
    if existing or not setup_is_eligible(setup):
        return existing
    symbol = db.get(Symbol, setup.symbol_id)
    distance = abs(D(setup.preferred_entry) - D(setup.stop_loss))
    if not symbol or distance <= 0:
        return None
    quantity = D("1") / distance  # one normalized quote-currency risk unit
    row = PaperForwardTrade(
        setup_id=setup.id, symbol_id=setup.symbol_id, symbol=symbol.symbol,
        strategy=setup.strategy, direction=setup.direction, timeframe=setup.setup_timeframe,
        confidence_score=setup.confidence_score, simulated_entry=setup.preferred_entry,
        entry_min=setup.entry_min, entry_max=setup.entry_max, stop_loss=setup.stop_loss,
        active_stop=setup.stop_loss, take_profit_1=setup.take_profit_1,
        take_profit_2=setup.take_profit_2, take_profit_3=setup.take_profit_3,
        initial_quantity=quantity, remaining_quantity=quantity, risk_amount=1,
        fee_rate_pct=fee_rate_pct, status="waiting_entry", market_data_source=SOURCE,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return db.scalar(select(PaperForwardTrade).where(PaperForwardTrade.setup_id == setup.id))
    return row


def _target(trade: PaperForwardTrade):
    for number in range(trade.next_target, 4):
        value = getattr(trade, f"take_profit_{number}")
        if value is not None:
            return number, D(value)
    return None, None


def _record_exit(trade: PaperForwardTrade, price: Decimal, quantity: Decimal, reason: str, when: datetime) -> None:
    fee = execution_fee(price, quantity, D(trade.fee_rate_pct))
    trade.fees += fee
    trade.realized_pnl += pnl(trade.direction, D(trade.simulated_entry), price, quantity, fee)
    trade.realized_r = trade.realized_pnl / D(trade.risk_amount)
    trade.remaining_quantity -= quantity
    trade.exit_price, trade.exit_reason = price, reason
    if trade.remaining_quantity <= D("0.000000000001"):
        trade.remaining_quantity = 0
        trade.status, trade.closed_at = "closed", when
    else:
        trade.status = "partially_closed"


def process_trade_candle(trade: PaperForwardTrade, setup: TradeSetup, candle: Candle) -> bool:
    """Process at most one exit event per candle to avoid favorable intrabar assumptions."""
    if trade.status in TERMINAL or candle.timeframe != trade.timeframe or candle.symbol_id != trade.symbol_id or not candle.is_closed:
        return False
    if candle.open_time < setup.detected_at:
        return False
    if trade.status == "waiting_entry":
        if candle.open_time > setup.expires_at:
            trade.status, trade.exit_reason = "expired", "setup_expired"
            return True
        if D(candle.high) < D(trade.entry_min) or D(candle.low) > D(trade.entry_max):
            return False
        trade.status, trade.opened_at = "open", candle.open_time
        entry_fee = execution_fee(D(trade.simulated_entry), D(trade.initial_quantity), D(trade.fee_rate_pct))
        trade.fees, trade.realized_pnl = entry_fee, -entry_fee
        trade.realized_r = trade.realized_pnl / D(trade.risk_amount)

    trade.holding_bars += 1
    favorable = (D(candle.high) - D(trade.simulated_entry) if trade.direction == "bullish" else D(trade.simulated_entry) - D(candle.low))
    adverse = (D(trade.simulated_entry) - D(candle.low) if trade.direction == "bullish" else D(candle.high) - D(trade.simulated_entry))
    trade.max_favorable_excursion = max(D(trade.max_favorable_excursion), favorable, D("0"))
    trade.max_adverse_excursion = max(D(trade.max_adverse_excursion), adverse, D("0"))
    distance = abs(D(trade.simulated_entry) - D(trade.stop_loss))
    trade.mfe_r = trade.max_favorable_excursion / distance
    trade.mae_r = trade.max_adverse_excursion / distance
    number, target = _target(trade)
    if target is None:
        _record_exit(trade, D(candle.close), D(trade.remaining_quantity), "targets_completed", candle.close_time)
        return True
    event = candle_exit(trade.direction, D(candle.high), D(candle.low), D(trade.active_stop), target, "stop_first")
    if event.price is None:
        return True
    trade.is_ambiguous = trade.is_ambiguous or event.ambiguous
    if event.reason == "stop_loss":
        _record_exit(trade, D(event.price), D(trade.remaining_quantity), "stop_loss", candle.close_time)
        return True
    has_later_target = any(getattr(trade, f"take_profit_{n}") is not None for n in range(number + 1, 4))
    quantity = D(trade.remaining_quantity) if not has_later_target else min(D(trade.remaining_quantity), D(trade.initial_quantity) * TP_FRACTIONS[number])
    _record_exit(trade, target, quantity, f"tp{number}", candle.close_time)
    trade.next_target = number + 1
    if number == 1 and trade.status != "closed":
        trade.active_stop = trade.simulated_entry
    if trade.next_target > 3 and trade.status != "closed":
        _record_exit(trade, target, D(trade.remaining_quantity), "tp3", candle.close_time)
    return True


def process_paper_forward_candle(db: Session, candle: Candle) -> list[PaperForwardTrade]:
    setups = list(db.scalars(select(TradeSetup).where(
        TradeSetup.symbol_id == candle.symbol_id,
        TradeSetup.setup_timeframe == candle.timeframe,
        TradeSetup.detected_at <= candle.close_time,
    )))
    for setup in setups:
        enroll_setup(db, setup)
    changed = []
    rows = list(db.scalars(select(PaperForwardTrade).where(
        PaperForwardTrade.symbol_id == candle.symbol_id,
        PaperForwardTrade.timeframe == candle.timeframe,
        PaperForwardTrade.status.in_(["waiting_entry", "open", "partially_closed"]),
    )))
    for trade in rows:
        setup = db.get(TradeSetup, trade.setup_id)
        if setup and process_trade_candle(trade, setup, candle):
            changed.append(trade)
    return changed


def backfill(db: Session, symbol: str | None, start: datetime, end: datetime, apply: bool = False) -> dict:
    query = select(TradeSetup).join(Symbol, Symbol.id == TradeSetup.symbol_id).where(
        TradeSetup.detected_at >= start, TradeSetup.detected_at <= end
    )
    if symbol:
        query = query.where(Symbol.symbol == symbol.upper())
    setups = list(db.scalars(query.order_by(TradeSetup.detected_at, TradeSetup.id)))
    existing = set(db.scalars(select(PaperForwardTrade.setup_id).where(PaperForwardTrade.setup_id.in_([s.id for s in setups])))) if setups else set()
    eligible = [s for s in setups if setup_is_eligible(s)]
    report = {"setups_scanned": len(setups), "eligible": len(eligible), "duplicates": sum(s.id in existing for s in eligible), "created": 0, "processed_candles": 0, "dry_run": not apply, "market_data_source": SOURCE}
    if not apply:
        return report
    for setup in eligible:
        if setup.id not in existing and enroll_setup(db, setup):
            report["created"] += 1
    candles = list(db.scalars(select(Candle).join(Symbol, Symbol.id == Candle.symbol_id).where(
        Candle.is_closed.is_(True), Candle.close_time >= start, Candle.open_time <= end,
        *( [Symbol.symbol == symbol.upper()] if symbol else [] ),
    ).order_by(Candle.close_time, Candle.id)))
    for candle in candles:
        process_paper_forward_candle(db, candle)
    report["processed_candles"] = len(candles)
    db.commit()
    return report


def comparison_rows(db: Session, trades: list[PaperForwardTrade]) -> list[dict]:
    result = []
    for trade in trades:
        live = db.scalar(select(LivePosition).where(
            LivePosition.originating_trade_setup_id == trade.setup_id,
            LivePosition.environment == "testnet",
        ).order_by(LivePosition.id.desc()).limit(1))
        actual = D(live.exit_price) if live and live.exit_price is not None else None
        risk_distance = abs(D(trade.simulated_entry) - D(trade.stop_loss))
        testnet_r = None if actual is None or not risk_distance else ((actual - D(trade.simulated_entry)) if trade.direction == "bullish" else (D(trade.simulated_entry) - actual)) / risk_distance
        slippage = None if actual is None else ((D(trade.stop_loss) - actual) if trade.direction == "bullish" else (actual - D(trade.stop_loss)))
        result.append({
            "setup_id": trade.setup_id, "paper_result": {"status": trade.status, "exit_reason": trade.exit_reason, "realized_r": trade.realized_r, "realized_pnl": trade.realized_pnl},
            "testnet_result": None if not live else {"position_id": live.id, "status": live.status, "exit_reason": live.exit_reason, "realized_pnl": live.realized_pnl},
            "intended_sl": trade.stop_loss, "testnet_actual_exit": actual, "testnet_slippage": slippage,
            "difference_in_r": None if testnet_r is None else testnet_r - D(trade.realized_r),
        })
    return result
