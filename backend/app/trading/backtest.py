from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BacktestRun, BacktestTrade, Candle, TradeSetup
from app.trading.execution import candle_exit, execution_fee, pnl, position_size, setup_available, slipped_price
from app.trading.metrics import calculate_metrics
from app.trading.validation import validate_setup


def run_backtest(db: Session, run: BacktestRun) -> BacktestRun:
    settings = run.settings_json
    run.status, run.started_at = "running", datetime.now(timezone.utc)
    query = select(TradeSetup).where(
        TradeSetup.symbol_id == run.symbol_id,
        TradeSetup.setup_timeframe == run.timeframe,
        TradeSetup.detected_at >= run.start_time,
        TradeSetup.detected_at <= run.end_time,
    )
    if run.strategy != "ALL":
        query = query.where(TradeSetup.strategy == run.strategy)
    setups = list(db.scalars(query.order_by(TradeSetup.detected_at, TradeSetup.id)))
    candles = list(db.scalars(select(Candle).where(
        Candle.symbol_id == run.symbol_id, Candle.timeframe == run.timeframe,
        Candle.open_time >= run.start_time, Candle.close_time <= run.end_time,
        Candle.is_closed.is_(True),
    ).order_by(Candle.open_time, Candle.id)))
    run.total_setups = len(setups)
    balance = Decimal(run.starting_balance)
    pnls: list[Decimal] = []
    rs: list[Decimal] = []
    for setup in setups:
        if setup.status == "rejected" or not validate_setup(setup).valid:
            continue
        future = [c for c in candles if setup_available(setup, c) and c.open_time >= setup.detected_at]
        entry_candle = next((c for c in future if c.low <= setup.preferred_entry <= c.high), None)
        if not entry_candle:
            continue
        entry = slipped_price(Decimal(setup.preferred_entry), setup.direction, Decimal(str(settings["slippage_bps"])), True)
        risk_amount, quantity = position_size(balance, Decimal(run.risk_per_trade_pct), entry, Decimal(setup.stop_loss), Decimal(run.risk_per_trade_pct))
        target = next((Decimal(x) for x in (setup.take_profit_3, setup.take_profit_2, setup.take_profit_1) if x is not None), None)
        if target is None:
            continue
        exit_candle = exit_event = None
        holding = 0
        for holding, candle in enumerate(future[future.index(entry_candle):], 1):
            event = candle_exit(setup.direction, Decimal(candle.high), Decimal(candle.low), Decimal(setup.stop_loss), target, settings["same_candle_policy"])
            if event.price is not None:
                exit_candle, exit_event = candle, event
                break
        if exit_candle is None:
            exit_candle = future[-1]
            raw_exit, reason = Decimal(exit_candle.close), "end_of_test"
        else:
            raw_exit, reason = Decimal(exit_event.price), exit_event.reason
        exit_price = slipped_price(raw_exit, setup.direction, Decimal(str(settings["slippage_bps"])), False)
        fees = execution_fee(entry, quantity, Decimal(str(settings["taker_fee_pct"]))) + execution_fee(exit_price, quantity, Decimal(str(settings["taker_fee_pct"])))
        realized = pnl(setup.direction, entry, exit_price, quantity, fees)
        realized_r = realized / risk_amount
        trade = BacktestTrade(
            backtest_run_id=run.id, trade_setup_id=setup.id, direction=setup.direction,
            entry_time=entry_candle.open_time, entry_price=entry, stop_loss=setup.stop_loss,
            take_profit_1=setup.take_profit_1, take_profit_2=setup.take_profit_2, take_profit_3=setup.take_profit_3,
            exit_time=exit_candle.close_time, exit_price=exit_price, exit_reason=reason,
            risk_amount=risk_amount, quantity=quantity, fees=fees,
            slippage=abs(entry - Decimal(setup.preferred_entry)) * quantity + abs(exit_price - raw_exit) * quantity,
            realized_pnl=realized, realized_r=realized_r, mae=0, mfe=0, holding_bars=holding,
        )
        db.add(trade)
        balance += realized
        pnls.append(realized)
        rs.append(realized_r)
    metrics = calculate_metrics(pnls, rs, Decimal(run.starting_balance))
    for key in ("trades_taken", "wins", "losses", "break_even", "gross_profit", "gross_loss", "net_profit", "profit_factor", "win_rate", "max_drawdown_pct", "expectancy", "average_rr", "sharpe_like_ratio"):
        setattr(run, key, metrics[key])
    run.settings_json = {**settings, "equity_curve": metrics["equity_curve"]}
    run.status, run.completed_at = "completed", datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run
