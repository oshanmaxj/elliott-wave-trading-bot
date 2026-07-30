"""Isolated deterministic historical replay using the production analysis pipeline."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models import (
    BacktestRun, BacktestTrade, Candle, ElliottWaveCount, FVGZone,
    LiquidityPool, LiquiditySweep, MarketStructureEvent, OrderBlock, Setting,
    SwingPoint, Symbol, TradeSetup,
)
from app.services.pipeline import process_closed_candle
from app.trading.execution import candle_exit, execution_fee, pnl, position_size, slipped_price
from app.trading.metrics import calculate_metrics
from app.trading.validation import validate_setup

WARMUP_BARS = 250


class CandleCoverageError(ValueError):
    def __init__(self, coverage: dict):
        self.coverage = coverage
        super().__init__(
            "Insufficient historical candle coverage: "
            f"requested {coverage['requested_from']} to {coverage['requested_to']}; "
            f"available {coverage['available_from']} to {coverage['available_to']} "
            f"({coverage['candle_count']} candles in range)"
        )


def candle_coverage(db: Session, symbol_id: int, timeframe: str, start: datetime, end: datetime) -> dict:
    available_from, available_to = db.execute(
        select(func.min(Candle.open_time), func.max(Candle.close_time)).where(
            Candle.symbol_id == symbol_id, Candle.timeframe == timeframe,
            Candle.is_closed.is_(True),
        )
    ).one()
    count = db.scalar(select(func.count(Candle.id)).where(
        Candle.symbol_id == symbol_id, Candle.timeframe == timeframe,
        Candle.is_closed.is_(True), Candle.close_time >= start, Candle.close_time <= end,
    )) or 0
    return {
        "available_from": available_from.isoformat() if available_from else None,
        "available_to": available_to.isoformat() if available_to else None,
        "requested_from": start.isoformat(), "requested_to": end.isoformat(),
        "candle_count": count,
    }


def closed_candles_at(candles: list[Candle], timestamp: datetime) -> list[Candle]:
    """The sole replay visibility boundary, also useful for regression tests."""
    return [c for c in candles if c.is_closed and c.close_time <= timestamp]


def _clone_candle(row: Candle) -> Candle:
    return Candle(
        id=row.id, symbol_id=row.symbol_id, timeframe=row.timeframe,
        open_time=row.open_time, close_time=row.close_time, open=row.open, high=row.high,
        low=row.low, close=row.close, volume=row.volume, quote_volume=row.quote_volume,
        trade_count=row.trade_count, taker_buy_base_volume=row.taker_buy_base_volume,
        taker_buy_quote_volume=row.taker_buy_quote_volume, is_closed=True,
    )


def _replay_analysis(db: Session, run: BacktestRun):
    coverage = candle_coverage(db, run.symbol_id, run.timeframe, run.start_time, run.end_time)
    if not coverage["candle_count"] or coverage["available_from"] is None:
        raise CandleCoverageError(coverage)
    available_from = datetime.fromisoformat(coverage["available_from"])
    available_to = datetime.fromisoformat(coverage["available_to"])
    start, end = run.start_time, run.end_time
    if available_from.tzinfo is None and start.tzinfo is not None:
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    if available_from > start or available_to < end:
        raise CandleCoverageError(coverage)

    entry_rows = list(db.scalars(select(Candle).where(
        Candle.symbol_id == run.symbol_id, Candle.timeframe == run.timeframe,
        Candle.is_closed.is_(True), Candle.close_time <= run.end_time,
    ).order_by(Candle.close_time, Candle.id)))
    first_requested = next((i for i, c in enumerate(entry_rows) if c.close_time >= start), len(entry_rows))
    entry_rows = entry_rows[max(0, first_requested - WARMUP_BARS):]
    warmup_from = entry_rows[0].open_time if entry_rows else start
    htf_rows = [] if run.timeframe == "4h" else list(db.scalars(select(Candle).where(
        Candle.symbol_id == run.symbol_id, Candle.timeframe == "4h",
        Candle.is_closed.is_(True), Candle.close_time >= warmup_from - timedelta(days=60),
        Candle.close_time <= run.end_time,
    ).order_by(Candle.close_time, Candle.id)))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    symbol = db.get(Symbol, run.symbol_id)
    setting = db.scalar(select(Setting).where(Setting.key == "runtime"))
    with factory() as replay:
        replay.add(Symbol(
            id=symbol.id, exchange=symbol.exchange, symbol=symbol.symbol,
            base_asset=symbol.base_asset, quote_asset=symbol.quote_asset,
            market_type=symbol.market_type, is_active=symbol.is_active,
        ))
        if setting:
            replay.add(Setting(key=setting.key, value_json=setting.value_json))
        replay.commit()
        # A candle is inserted only at its close. Partially formed HTF candles can
        # therefore never be queried by the production pipeline.
        for candle in sorted(entry_rows + htf_rows, key=lambda c: (c.close_time, c.timeframe, c.id)):
            replay.add(_clone_candle(candle))
            replay.commit()
            asyncio.run(process_closed_candle(candle.id, broadcast=False, session_factory=factory))
    requested = [c for c in entry_rows if c.close_time >= start and c.close_time <= end]
    return factory, requested, coverage


def _count(replay, model, run, *extra):
    return replay.scalar(select(func.count(model.id)).where(
        model.symbol_id == run.symbol_id, model.timeframe == run.timeframe,
        model.detected_at >= run.start_time, model.detected_at <= run.end_time, *extra,
    )) or 0


def _diagnostics(replay: Session, run: BacktestRun, candle_count: int):
    setups = list(replay.scalars(select(TradeSetup).where(
        TradeSetup.symbol_id == run.symbol_id,
        TradeSetup.setup_timeframe == run.timeframe,
        TradeSetup.detected_at >= run.start_time, TradeSetup.detected_at <= run.end_time,
    ).order_by(TradeSetup.detected_at, TradeSetup.id)))
    if run.strategy != "ALL":
        setups = [s for s in setups if s.strategy == run.strategy]
    rejected = [s for s in setups if s.rejection_reasons_json]
    reasons = Counter(r for s in rejected for r in (s.rejection_reasons_json or []))
    diagnostics = {
        "candles_processed": candle_count,
        "swings_detected": _count(replay, SwingPoint, run),
        "bos_detected": _count(replay, MarketStructureEvent, run, MarketStructureEvent.event_type == "BOS"),
        "choch_detected": _count(replay, MarketStructureEvent, run, MarketStructureEvent.event_type == "CHoCH"),
        "fvgs_detected": _count(replay, FVGZone, run),
        "liquidity_pools_detected": _count(replay, LiquidityPool, run),
        "liquidity_sweeps_detected": _count(replay, LiquiditySweep, run),
        "order_blocks_detected": _count(replay, OrderBlock, run),
        "elliott_counts_generated": _count(replay, ElliottWaveCount, run),
        "setup_candidates": len(setups), "setups_rejected": len(rejected),
        "setups_eligible": len(setups) - len(rejected), "entries_triggered": 0,
        "trades_completed": 0, "rejection_reasons": dict(reasons),
    }
    return diagnostics, setups


def run_backtest(db: Session, run: BacktestRun) -> BacktestRun:
    settings = run.settings_json
    run.status, run.started_at = "running", datetime.now(timezone.utc)
    try:
        factory, candles, coverage = _replay_analysis(db, run)
    except CandleCoverageError:
        run.status = "failed"
        db.commit()
        raise

    with factory() as replay:
        diagnostics, setups = _diagnostics(replay, run, len(candles))
        balance = Decimal(run.starting_balance)
        pnls, rs = [], []
        total_fees = Decimal("0")
        occupied_until = None
        for setup in setups:
            if setup.rejection_reasons_json or not validate_setup(setup).valid:
                continue
            future = [
                c for c in candles if c.open_time >= setup.detected_at
                and (setup.expires_at is None or c.open_time <= setup.expires_at)
                and (occupied_until is None or c.open_time > occupied_until)
            ]
            entry_candle = next((c for c in future if c.high >= setup.entry_min and c.low <= setup.entry_max), None)
            if not entry_candle:
                reasons = diagnostics["rejection_reasons"]
                reasons["entry_not_reached"] = reasons.get("entry_not_reached", 0) + 1
                continue
            raw_entry = Decimal(setup.preferred_entry)
            entry = slipped_price(raw_entry, setup.direction, Decimal(str(settings["slippage_bps"])), True)
            risk_amount, quantity = position_size(
                balance, Decimal(run.risk_per_trade_pct), entry, Decimal(setup.stop_loss),
                Decimal(run.risk_per_trade_pct),
            )
            target = next((Decimal(x) for x in (setup.take_profit_3, setup.take_profit_2, setup.take_profit_1) if x is not None), None)
            if target is None:
                diagnostics["rejection_reasons"]["missing_target"] = diagnostics["rejection_reasons"].get("missing_target", 0) + 1
                continue
            diagnostics["entries_triggered"] += 1
            path = future[future.index(entry_candle):]
            exit_candle, raw_exit, reason, holding = None, None, "end_of_test", 0
            for holding, candle in enumerate(path, 1):
                event = candle_exit(
                    setup.direction, Decimal(candle.high), Decimal(candle.low),
                    Decimal(setup.stop_loss), target, settings["same_candle_policy"],
                )
                if event.price is not None:
                    exit_candle, raw_exit, reason = candle, Decimal(event.price), event.reason
                    break
            if exit_candle is None:
                exit_candle, raw_exit = path[-1], Decimal(path[-1].close)
            exit_price = slipped_price(raw_exit, setup.direction, Decimal(str(settings["slippage_bps"])), False)
            fees = execution_fee(entry, quantity, Decimal(str(settings["taker_fee_pct"]))) + execution_fee(exit_price, quantity, Decimal(str(settings["taker_fee_pct"])))
            realized = pnl(setup.direction, entry, exit_price, quantity, fees)
            realized_r = realized / risk_amount
            db.add(BacktestTrade(
                backtest_run_id=run.id, trade_setup_id=None, direction=setup.direction,
                entry_time=entry_candle.open_time, entry_price=entry, stop_loss=setup.stop_loss,
                take_profit_1=setup.take_profit_1, take_profit_2=setup.take_profit_2,
                take_profit_3=setup.take_profit_3, exit_time=exit_candle.close_time,
                exit_price=exit_price, exit_reason=reason, risk_amount=risk_amount,
                quantity=quantity, fees=fees,
                slippage=(abs(entry - raw_entry) + abs(exit_price - raw_exit)) * quantity,
                realized_pnl=realized, realized_r=realized_r, mae=0, mfe=0, holding_bars=holding,
            ))
            occupied_until = exit_candle.close_time
            balance += realized
            total_fees += fees
            pnls.append(realized)
            rs.append(realized_r)

    metrics = calculate_metrics(pnls, rs, Decimal(run.starting_balance))
    for key in ("trades_taken", "wins", "losses", "break_even", "gross_profit", "gross_loss", "net_profit", "profit_factor", "win_rate", "max_drawdown_pct", "expectancy", "average_rr", "sharpe_like_ratio"):
        setattr(run, key, metrics[key])
    diagnostics["trades_completed"] = len(pnls)
    run.total_setups = diagnostics["setup_candidates"]
    run.settings_json = {
        **settings, "coverage": coverage, "diagnostics": diagnostics,
        "equity_curve": metrics["equity_curve"], "drawdown_curve": metrics["drawdown_curve"],
        "results": {**metrics["extended"], "total_fees": str(total_fees)},
        "same_candle_policy_note": "stop_first is conservative when SL and TP are both touched without intrabar data",
        "analysis_source": "isolated replay of the production closed-candle pipeline",
    }
    run.status, run.completed_at = "completed", datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run
