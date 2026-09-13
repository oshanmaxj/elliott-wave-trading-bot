"""Idempotent forward testing against persisted Binance production Spot candles only."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.execution.runtime import runtime_state
from app.models import Candle, ElliottWaveCount, LivePosition, PaperForwardTrade, Symbol, TradeSetup
from app.strategies import elliott_wave3_heikin_ashi as wave3_ha
from app.strategies.heikin_ashi import confirmed_reversal, derive_heikin_ashi
from app.trading.execution import candle_exit, execution_fee, pnl
from app.trading.validation import validate_setup

D = Decimal
SOURCE = "binance_production_spot_db"
TERMINAL = {"closed", "expired", "invalidated"}
TP_FRACTIONS = {1: D("0.30"), 2: D("0.40"), 3: D("0.30")}
WAVE3_HA_STRATEGY = wave3_ha.STRATEGY


def setup_is_eligible(setup: TradeSetup) -> bool:
    required = (setup.preferred_entry, setup.entry_min, setup.entry_max, setup.stop_loss)
    if setup.rejection_reasons_json or any(value is None for value in required):
        return False
    if setup.strategy == WAVE3_HA_STRATEGY:
        # This strategy has no fixed TP ladder by design: it rides the
        # position until a signal-driven exit (5m opposite HA reversal), a
        # hard structural stop, or an Elliott Wave-3 invalidation. The
        # generic reward-to-risk geometry check below assumes at least one
        # take-profit target exists, so it does not apply here - only
        # entry/stop geometry sanity is checked instead.
        entry, stop = D(setup.preferred_entry), D(setup.stop_loss)
        entry_in_zone = D(setup.entry_min) <= entry <= D(setup.entry_max)
        stop_on_correct_side = (
            stop < D(setup.entry_min) if setup.direction == "bullish" else stop > D(setup.entry_max)
        )
        return entry_in_zone and stop_on_correct_side and abs(entry - stop) > 0
    if not any(value is not None for value in (setup.take_profit_1, setup.take_profit_2, setup.take_profit_3)):
        return False
    return validate_setup(setup).valid


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


def _wave3_ha_1m_update(db: Session, trade: PaperForwardTrade, setup: TradeSetup, candle: Candle) -> bool:
    """Hard-stop and Elliott Wave-3 invalidation checks only - no TP ladder.

    Hard structural protection always wins same-candle ambiguity against a
    same-bar invalidation, mirroring the existing Wave3-HA research replay.
    """
    stopped = (
        D(candle.low) <= D(trade.active_stop)
        if trade.direction == "bullish"
        else D(candle.high) >= D(trade.active_stop)
    )
    if stopped:
        _record_exit(trade, D(trade.active_stop), D(trade.remaining_quantity), wave3_ha.EXIT_REASON_HARD_STOP, candle.close_time)
        return True
    count = db.get(ElliottWaveCount, setup.elliott_wave_count_id) if setup.elliott_wave_count_id else None
    if count and not wave3_ha.wave3_still_intact(count, D(candle.close)):
        _record_exit(trade, D(candle.close), D(trade.remaining_quantity), wave3_ha.EXIT_REASON_INVALIDATED, candle.close_time)
    return True


def _wave3_ha_5m_exit(db: Session, trade: PaperForwardTrade, candle: Candle, config: "wave3_ha.Config") -> bool:
    """Exit on a confirmed opposite 5m Heikin Ashi reversal (closed candles only).

    Only reads 5m candles closed at or before `candle`, and only acts when
    `candle` itself is the confirmation candle - a later close can never
    change whether an earlier candle triggered this exit.
    """
    m5 = list(db.scalars(select(Candle).where(
        Candle.symbol_id == candle.symbol_id, Candle.timeframe == wave3_ha.STRUCTURE_TIMEFRAME,
        Candle.is_closed.is_(True), Candle.open_time <= candle.open_time,
    ).order_by(Candle.open_time.desc()).limit(60)))
    m5.reverse()
    if len(m5) < 20 or m5[-1].id != candle.id:
        return False
    ha5 = derive_heikin_ashi(m5)
    opposite_direction = "bearish" if trade.direction == "bullish" else "bullish"
    reversal = confirmed_reversal(
        ha5, m5, len(ha5) - 1, opposite_direction,
        pullback_min=config.ha_pullback_min_candles,
        wick_body_max_ratio=config.ha_wick_body_max_ratio,
        body_atr_min_ratio=config.ha_body_atr_min_ratio,
        confirmation_required=config.ha_confirmation_required,
    )
    if not reversal:
        return False
    _record_exit(trade, D(reversal["real_entry"]), D(trade.remaining_quantity), wave3_ha.EXIT_REASON_HA_REVERSAL, candle.close_time)
    trade.exit_signal_candle_id = reversal["confirmation_candle_id"]
    return True


def process_trade_candle(trade: PaperForwardTrade, setup: TradeSetup, candle: Candle, db: Session = None) -> bool:
    """Process at most one exit event per candle to avoid favorable intrabar assumptions.

    `db` is optional and only used by the elliott_wave3_heikin_ashi branch
    (to look up its Elliott Wave count for the invalidation check) - every
    other strategy's call path is unchanged from before that branch existed.
    """
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
    if trade.strategy == WAVE3_HA_STRATEGY:
        return _wave3_ha_1m_update(db, trade, setup, candle) if db is not None else True
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
        if setup and process_trade_candle(trade, setup, candle, db):
            changed.append(trade)
    # elliott_wave3_heikin_ashi's exit signal lives on 5m candles while the
    # trade itself is recorded on the 1m entry timeframe, so it is checked
    # independently of the single-timeframe loop above.
    if candle.timeframe == wave3_ha.EXIT_TIMEFRAME:
        runtime = runtime_state(db)
        config = wave3_ha.load_config(runtime.strategy_config_json if runtime else {})
        open_wave3_trades = list(db.scalars(select(PaperForwardTrade).where(
            PaperForwardTrade.symbol_id == candle.symbol_id,
            PaperForwardTrade.strategy == WAVE3_HA_STRATEGY,
            PaperForwardTrade.status.in_(["open", "partially_closed"]),
        )))
        for trade in open_wave3_trades:
            if trade not in changed and _wave3_ha_5m_exit(db, trade, candle, config):
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
