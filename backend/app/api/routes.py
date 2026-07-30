from datetime import datetime
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES
from app.database.session import get_db
from app.market_data.binance_ws import market_stream
from app.elliott.service import recalculate_elliott
from app.models import (
    Alert,
    AnalysisSnapshot,
    BotLog,
    Candle,
    ElliottWaveCount,
    FVGZone,
    LiquidityPool,
    LiquiditySweep,
    MarketStructureEvent,
    OrderBlock,
    SwingPoint,
    Symbol,
    TradeSetup,
    BacktestRun,
    BacktestTrade,
    PaperAccount,
    PaperPosition,
)
from app.schemas.common import (
    AlertOut,
    AnalysisBackfillReport,
    AnalysisBackfillRequest,
    AnalysisBackfillStatusOut,
    AnalysisOut,
    BotLogOut,
    CandleOut,
    ElliottRecalculateRequest,
    ElliottWaveCountOut,
    FVGOut,
    LiquidityOut,
    LiquiditySweepOut,
    MarketBiasOut,
    OrderBlockOut,
    PremiumDiscountOut,
    RuntimeSettings,
    StructureOut,
    StructureScoreOut,
    SwingOut,
    SymbolOut,
    SyncRequest,
    TradeSetupOut,
    TradeSetupSummary,
    BacktestRequest,
    PaperAccountRequest,
    PaperAccountResetRequest,
    PaperCloseRequest,
    PaperTradeRequest,
)
from app.services.analysis_backfill import AnalysisBackfillService, backfill_status
from app.services.broadcast import broadcaster
from app.services.historical_sync import HistoricalSyncService
from app.services.settings import get_runtime_settings, save_runtime_settings
from app.smc.engine import multi_timeframe_bias, premium_discount, structure_score
from app.trading.backtest import CandleCoverageError, run_backtest
from app.trading.execution import execution_fee, position_size, slipped_price
from app.trading.metrics import calculate_metrics
from app.trading.paper import manual_close
from app.trading.validation import validate_setup

router = APIRouter(prefix="/api")


def resolve_symbol(db: Session, value: str) -> Symbol:
    value = value.upper()
    if value not in SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=422, detail="Unsupported symbol")
    row = db.scalar(select(Symbol).where(Symbol.symbol == value))
    if not row:
        raise HTTPException(status_code=404, detail="Symbol not initialized")
    return row


def validate_timeframe(value: str) -> str:
    if value not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(status_code=422, detail="Unsupported timeframe")
    return value


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "healthy", "market_stream": market_stream.status()}


@router.get("/symbols", response_model=list[SymbolOut])
def symbols(db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(Symbol).where(Symbol.is_active.is_(True)).order_by(Symbol.symbol)
        )
    )


@router.get("/candles", response_model=list[CandleOut])
def candles(
    symbol: str,
    timeframe: str,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(500, ge=1, le=1500),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    query = select(Candle).where(
        Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe
    )
    if start_time:
        query = query.where(Candle.open_time >= start_time)
    if end_time:
        query = query.where(Candle.open_time <= end_time)
    rows = list(db.scalars(query.order_by(Candle.open_time.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/swings", response_model=list[SwingOut])
def swings(
    symbol: str,
    timeframe: str,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    rows = list(
        db.scalars(
            select(SwingPoint)
            .where(
                SwingPoint.symbol_id == symbol_row.id, SwingPoint.timeframe == timeframe
            )
            .order_by(SwingPoint.detected_at.desc())
            .limit(limit)
        )
    )
    return list(reversed(rows))


@router.get("/structure", response_model=list[StructureOut])
def structure(
    symbol: str,
    timeframe: str,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    rows = list(
        db.scalars(
            select(MarketStructureEvent)
            .where(
                MarketStructureEvent.symbol_id == symbol_row.id,
                MarketStructureEvent.timeframe == timeframe,
            )
            .order_by(MarketStructureEvent.detected_at.desc())
            .limit(limit)
        )
    )
    return list(reversed(rows))


@router.get("/fvg", response_model=list[FVGOut])
def fvg(
    symbol: str,
    timeframe: str,
    direction: str | None = Query(None, pattern="^(bullish|bearish)$"),
    zone_status: str | None = Query(
        None,
        alias="status",
        pattern="^(active|partially_mitigated|fully_mitigated|invalidated)$",
    ),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    query = select(FVGZone).where(
        FVGZone.symbol_id == symbol_row.id, FVGZone.timeframe == timeframe
    )
    if direction:
        query = query.where(FVGZone.direction == direction)
    if zone_status:
        query = query.where(FVGZone.status == zone_status)
    rows = list(db.scalars(query.order_by(FVGZone.detected_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/analysis/latest", response_model=AnalysisOut)
def latest_analysis(symbol: str, timeframe: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    row = db.scalar(
        select(AnalysisSnapshot)
        .where(
            AnalysisSnapshot.symbol_id == symbol_row.id,
            AnalysisSnapshot.timeframe == timeframe,
        )
        .order_by(AnalysisSnapshot.generated_at.desc())
        .limit(1)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Analysis is not available yet")
    return row


@router.get("/liquidity", response_model=list[LiquidityOut])
def liquidity(
    symbol: str,
    timeframe: str,
    active_only: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    query = select(LiquidityPool).where(
        LiquidityPool.symbol_id == symbol_row.id, LiquidityPool.timeframe == timeframe
    )
    if active_only:
        query = query.where(LiquidityPool.swept_at.is_(None))
    rows = list(
        db.scalars(query.order_by(LiquidityPool.detected_at.desc()).limit(limit))
    )
    return list(reversed(rows))


@router.get("/order-blocks", response_model=list[OrderBlockOut])
def order_blocks(
    symbol: str,
    timeframe: str,
    block_status: str | None = Query(
        None,
        alias="status",
        pattern="^(active|partially_mitigated|fully_mitigated|invalidated)$",
    ),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    query = select(OrderBlock).where(
        OrderBlock.symbol_id == symbol_row.id, OrderBlock.timeframe == timeframe
    )
    if block_status:
        query = query.where(OrderBlock.status == block_status)
    rows = list(db.scalars(query.order_by(OrderBlock.detected_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/premium-discount", response_model=PremiumDiscountOut)
def premium_discount_zones(symbol: str, timeframe: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    swings = list(
        db.scalars(
            select(SwingPoint)
            .where(
                SwingPoint.symbol_id == symbol_row.id, SwingPoint.timeframe == timeframe
            )
            .order_by(SwingPoint.detected_at, SwingPoint.id)
        )
    )
    result = premium_discount(swings)
    if not result:
        raise HTTPException(
            status_code=404, detail="A confirmed swing range is not available yet"
        )
    return result


@router.get("/market-bias", response_model=MarketBiasOut)
def market_bias(symbol: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    trends = {}
    for timeframe in ("4h", "1h", "15m"):
        snapshot = db.scalar(
            select(AnalysisSnapshot)
            .where(
                AnalysisSnapshot.symbol_id == symbol_row.id,
                AnalysisSnapshot.timeframe == timeframe,
            )
            .order_by(AnalysisSnapshot.generated_at.desc())
            .limit(1)
        )
        trends[timeframe] = snapshot.trend if snapshot else "undefined"
    return {"symbol": symbol_row.symbol, **multi_timeframe_bias(trends)}


@router.get("/structure-score", response_model=StructureScoreOut)
def score(symbol: str, timeframe: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    snapshot = db.scalar(
        select(AnalysisSnapshot)
        .where(
            AnalysisSnapshot.symbol_id == symbol_row.id,
            AnalysisSnapshot.timeframe == timeframe,
        )
        .order_by(AnalysisSnapshot.generated_at.desc())
        .limit(1)
    )
    if not snapshot:
        raise HTTPException(status_code=404, detail="Analysis is not available yet")
    latest_event = db.scalar(
        select(MarketStructureEvent)
        .where(
            MarketStructureEvent.symbol_id == symbol_row.id,
            MarketStructureEvent.timeframe == timeframe,
        )
        .order_by(MarketStructureEvent.detected_at.desc())
        .limit(1)
    )
    liquidity_count = (
        db.scalar(
            select(func.count(LiquidityPool.id)).where(
                LiquidityPool.symbol_id == symbol_row.id,
                LiquidityPool.timeframe == timeframe,
                LiquidityPool.swept_at.is_(None),
            )
        )
        or 0
    )
    block_count = (
        db.scalar(
            select(func.count(OrderBlock.id)).where(
                OrderBlock.symbol_id == symbol_row.id,
                OrderBlock.timeframe == timeframe,
                OrderBlock.status.in_(["active", "partially_mitigated"]),
            )
        )
        or 0
    )
    result = structure_score(
        snapshot.trend,
        latest_event,
        liquidity_count,
        block_count,
        snapshot.active_fvg_count,
        snapshot.indicator_values_json,
    )
    return {"symbol": symbol_row.symbol, "timeframe": timeframe, **result}


@router.get("/alerts", response_model=list[AlertOut])
def alerts(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = select(Alert)
    if symbol:
        symbol_row = resolve_symbol(db, symbol)
        query = query.where(Alert.symbol_id == symbol_row.id)
    if timeframe:
        query = query.where(Alert.timeframe == validate_timeframe(timeframe))
    return list(db.scalars(query.order_by(Alert.created_at.desc()).limit(limit)))


@router.get("/liquidity-sweeps", response_model=list[LiquiditySweepOut])
def liquidity_sweeps(
    symbol: str,
    timeframe: str,
    direction: str | None = Query(None, pattern="^(bullish|bearish)$"),
    sweep_status: str | None = Query(
        None, alias="status", pattern="^(candidate|confirmed|invalidated|expired)$"
    ),
    minimum_confidence: float = Query(0, ge=0, le=100),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    query = select(LiquiditySweep).where(
        LiquiditySweep.symbol_id == symbol_row.id,
        LiquiditySweep.timeframe == timeframe,
        LiquiditySweep.confidence_score >= minimum_confidence,
    )
    if direction:
        query = query.where(LiquiditySweep.direction == direction)
    if sweep_status:
        query = query.where(LiquiditySweep.status == sweep_status)
    rows = list(
        db.scalars(query.order_by(LiquiditySweep.detected_at.desc()).limit(limit))
    )
    return list(reversed(rows))


@router.get("/liquidity-sweeps/{sweep_id}", response_model=LiquiditySweepOut)
def liquidity_sweep_detail(sweep_id: int, db: Session = Depends(get_db)):
    row = db.get(LiquiditySweep, sweep_id)
    if not row:
        raise HTTPException(status_code=404, detail="Liquidity sweep not found")
    return row


@router.get("/trade-setups", response_model=list[TradeSetupOut])
def trade_setups(
    symbol: str,
    direction: str | None = Query(None, pattern="^(bullish|bearish)$"),
    strategy: str | None = Query(
        None,
        pattern="^(bullish_liquidity_reversal|bearish_liquidity_reversal|bullish_continuation|bearish_continuation|bullish_wave_3|bearish_wave_3|bullish_wave_5|bearish_wave_5|bullish_c_wave|bearish_c_wave)$",
    ),
    setup_status: str | None = Query(
        None,
        alias="status",
        pattern="^(candidate|watching|ready|waiting_entry|triggered|rejected|expired|invalidated|cancelled|paper_traded)$",
    ),
    minimum_confidence: float = Query(0, ge=0, le=100),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    query = select(TradeSetup).where(
        TradeSetup.symbol_id == symbol_row.id,
        TradeSetup.confidence_score >= minimum_confidence,
    )
    if direction:
        query = query.where(TradeSetup.direction == direction)
    if strategy:
        query = query.where(TradeSetup.strategy == strategy)
    if setup_status:
        query = query.where(TradeSetup.status == setup_status)
    return list(db.scalars(query.order_by(TradeSetup.detected_at.desc()).limit(limit)))


@router.get("/trade-setups/summary", response_model=TradeSetupSummary)
def trade_setup_summary(symbol: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    base = TradeSetup.symbol_id == symbol_row.id
    latest = db.scalar(
        select(TradeSetup)
        .where(base, TradeSetup.status == "ready")
        .order_by(TradeSetup.detected_at.desc())
        .limit(1)
    )
    def count(*conditions):
        return db.scalar(select(func.count(TradeSetup.id)).where(base, *conditions)) or 0
    average = db.scalar(select(func.avg(TradeSetup.confidence_score)).where(base)) or 0
    return {
        "watching_count": count(TradeSetup.status == "watching"),
        "ready_count": count(TradeSetup.status == "ready"),
        "bullish_count": count(
            TradeSetup.direction == "bullish",
            TradeSetup.status.in_(["watching", "ready", "triggered"]),
        ),
        "bearish_count": count(
            TradeSetup.direction == "bearish",
            TradeSetup.status.in_(["watching", "ready", "triggered"]),
        ),
        "latest_ready_setup": latest,
        "average_confidence": float(average),
    }


@router.get("/trade-setups/{setup_id}")
def trade_setup_detail(setup_id: int, db: Session = Depends(get_db)):
    row = db.get(TradeSetup, setup_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trade setup not found")
    symbol = db.get(Symbol, row.symbol_id)
    structure = db.get(MarketStructureEvent, row.structure_event_id)
    sweep = db.get(LiquiditySweep, row.liquidity_sweep_id) if row.liquidity_sweep_id else None
    pool = db.get(LiquidityPool, sweep.liquidity_pool_id) if sweep else None
    fvg = db.get(FVGZone, row.fvg_zone_id) if row.fvg_zone_id else None
    block = db.get(OrderBlock, row.order_block_id) if row.order_block_id else None
    wave = db.get(ElliottWaveCount, row.elliott_wave_count_id) if row.elliott_wave_count_id else None
    runtime = get_runtime_settings(db)
    minimum_confidence = (
        runtime.counter_trend_minimum_confidence
        if row.setup_conditions_json.get("counter_trend")
        else runtime.minimum_setup_confidence
    )
    validation = validate_setup(row, Decimal(str(runtime.minimum_reward_to_risk)))
    checks = [
        {"rule": "Market bias alignment", "status": "FAIL" if row.setup_conditions_json.get("counter_trend") else "PASS", "actual": row.setup_conditions_json.get("counter_trend"), "required": False},
        {"rule": "HTF alignment", "status": "FAIL" if row.setup_conditions_json.get("counter_trend") and not runtime.counter_trend_setups_enabled else "PASS", "actual": row.setup_conditions_json.get("counter_trend"), "required": "aligned or counter-trend enabled"},
        {"rule": "Structure confirmation", "status": "PASS" if structure and structure.direction == row.direction else "FAIL", "actual": structure.event_type if structure else None, "required": "BOS/CHoCH aligned"},
        {"rule": "Liquidity confirmation", "status": "PASS" if sweep and sweep.status == "confirmed" else ("NOT APPLICABLE" if row.setup_conditions_json.get("stop_source") != "sweep" else "FAIL"), "actual": sweep.status if sweep else None, "required": "confirmed when required"},
        {"rule": "Elliott confirmation", "status": "PASS" if wave and not wave.rules_failed_json else ("NOT APPLICABLE" if not wave else "FAIL"), "actual": wave.confidence_score if wave else None, "required": runtime.elliott_minimum_confidence},
        {"rule": "Confidence", "status": "PASS" if row.confidence_score >= minimum_confidence else "FAIL", "actual": row.confidence_score, "required": minimum_confidence},
        {"rule": "Risk Reward", "status": "PASS" if validation.valid or "invalid_rr" not in validation.reasons else "FAIL", "actual": row.risk_reward_2, "required": runtime.minimum_reward_to_risk},
    ]
    return {
        "setup": row, "symbol": symbol.symbol, "market_bias": {
            "higher_timeframe": row.higher_timeframe,
            "counter_trend": row.setup_conditions_json.get("counter_trend"),
        },
        "structure": structure, "elliott": wave, "liquidity_sweep": sweep,
        "liquidity_pool": pool, "fvg": fvg, "order_block": block,
        "validation": {
            "valid": not row.rejection_reasons_json and validation.valid,
            "checklist": checks,
            "rejection_reasons": row.rejection_reasons_json or validation.reasons,
        },
    }


@router.post("/trade-setups/{setup_id}/reject", response_model=TradeSetupOut)
def reject_trade_setup(setup_id: int, db: Session = Depends(get_db)):
    row = db.get(TradeSetup, setup_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trade setup not found")
    if row.status in {"triggered", "paper_traded"}:
        raise HTTPException(
            status_code=409,
            detail="Triggered or paper-traded setups cannot be rejected",
        )
    row.status = "rejected"
    row.rejection_reasons_json = [*row.rejection_reasons_json, "manually rejected"]
    db.commit()
    db.refresh(row)
    return row


@router.post("/trade-setups/{setup_id}/paper-trade")
def paper_trade_setup(setup_id: int, body: PaperTradeRequest | None = None, db: Session = Depends(get_db)):
    setup = db.get(TradeSetup, setup_id)
    if not setup:
        raise HTTPException(status_code=404, detail="Trade setup not found")
    if body is None:
        raise HTTPException(status_code=422, detail="Paper account and execution settings are required")
    account = db.get(PaperAccount, body.account_id)
    if not account or not account.is_active:
        raise HTTPException(status_code=404, detail="Active paper account not found")
    validation = validate_setup(setup)
    if setup.status != "ready" or not validation.valid:
        raise HTTPException(status_code=409, detail={"message": "Setup is not eligible for paper execution", "reasons": validation.reasons})
    if db.scalar(select(PaperPosition).where(PaperPosition.trade_setup_id == setup.id, PaperPosition.status.in_(["waiting_entry", "pending", "open", "partially_closed"]))):
        raise HTTPException(status_code=409, detail="Setup already has an active paper position")
    risk_amount, quantity = position_size(account.equity, account.risk_per_trade_pct, setup.preferred_entry, setup.stop_loss, body.max_risk_per_trade_pct)
    entry = slipped_price(setup.preferred_entry, setup.direction, body.slippage_bps, True)
    fee = execution_fee(entry, quantity, body.taker_fee_pct)
    position = PaperPosition(
        account_id=account.id, trade_setup_id=setup.id, symbol_id=setup.symbol_id,
        direction=setup.direction, status="waiting_entry", entry_price=entry, quantity=quantity,
        initial_quantity=quantity, risk_amount=risk_amount,
        stop_loss=setup.stop_loss, tp1=setup.take_profit_1, tp2=setup.take_profit_2,
        tp3=setup.take_profit_3, realized_pnl=0, realized_r=0, fees=fee,
        slippage=abs(entry - setup.preferred_entry) * quantity,
        taker_fee_pct=body.taker_fee_pct, slippage_bps=body.slippage_bps,
    )
    db.add(position)
    setup.status = "waiting_entry"
    db.flush()
    db.add(BotLog(
        level="INFO", service="paper", event_type="setup_ready",
        message=f"Setup {setup.id} queued for paper entry",
        context_json={"trade_setup_id": setup.id, "paper_position_id": position.id, "account_id": account.id},
    ))
    db.commit()
    db.refresh(position)
    return {"position": position, "risk_amount": risk_amount, "paper_only": True}


@router.post("/backtests")
def create_backtest(body: BacktestRequest, db: Session = Depends(get_db)):
    symbol = resolve_symbol(db, body.symbol)
    validate_timeframe(body.timeframe)
    if body.start_time >= body.end_time:
        raise HTTPException(status_code=422, detail="start_time must be before end_time")
    run = BacktestRun(
        symbol_id=symbol.id, timeframe=body.timeframe, strategy=body.strategy,
        start_time=body.start_time, end_time=body.end_time,
        starting_balance=body.starting_balance, risk_per_trade_pct=body.risk_per_trade_pct,
        status="pending", settings_json={
            "maker_fee_pct": str(body.maker_fee_pct), "taker_fee_pct": str(body.taker_fee_pct),
            "slippage_bps": str(body.slippage_bps), "same_candle_policy": body.same_candle_policy,
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        return run_backtest(db, run)
    except CandleCoverageError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "coverage": exc.coverage},
        ) from exc


@router.get("/backtests")
def list_backtests(db: Session = Depends(get_db)):
    return list(db.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(100)))


@router.get("/backtests/{run_id}")
def backtest_detail(run_id: int, db: Session = Depends(get_db)):
    row = db.get(BacktestRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return row


@router.get("/backtests/{run_id}/trades")
def backtest_trades(run_id: int, db: Session = Depends(get_db)):
    return list(db.scalars(select(BacktestTrade).where(BacktestTrade.backtest_run_id == run_id).order_by(BacktestTrade.entry_time)))


@router.get("/backtests/{run_id}/equity")
def backtest_equity(run_id: int, db: Session = Depends(get_db)):
    row = db.get(BacktestRun, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return row.settings_json.get("equity_curve", [])


@router.get("/paper/accounts")
def paper_accounts(db: Session = Depends(get_db)):
    rows = list(db.scalars(select(PaperAccount).order_by(PaperAccount.created_at)))
    if not rows:
        account = PaperAccount(
            name="Default Paper Account", starting_balance=10000, balance=10000,
            equity=10000, realized_pnl=0, unrealized_pnl=0, max_equity=10000,
            drawdown_pct=0, risk_per_trade_pct=1, max_daily_loss_pct=3,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        rows = [account]
    for account in rows:
        unrealized = Decimal("0")
        positions = list(db.scalars(select(PaperPosition).where(
            PaperPosition.account_id == account.id,
            PaperPosition.status.in_(["open", "partially_closed"]),
        )))
        for position in positions:
            latest = db.scalar(select(Candle).where(
                Candle.symbol_id == position.symbol_id, Candle.is_closed.is_(True),
            ).order_by(Candle.close_time.desc()).limit(1))
            if latest:
                move = (
                    Decimal(latest.close) - Decimal(position.entry_price)
                    if position.direction == "bullish"
                    else Decimal(position.entry_price) - Decimal(latest.close)
                )
                unrealized += move * Decimal(position.quantity)
        account.unrealized_pnl = unrealized
        account.equity = account.balance + unrealized
    db.commit()
    return rows


@router.post("/paper/accounts")
def create_paper_account(body: PaperAccountRequest, db: Session = Depends(get_db)):
    account = PaperAccount(
        name=body.name, starting_balance=body.starting_balance, balance=body.starting_balance,
        equity=body.starting_balance, realized_pnl=0, unrealized_pnl=0,
        max_equity=body.starting_balance, drawdown_pct=0,
        risk_per_trade_pct=body.risk_per_trade_pct, max_daily_loss_pct=body.max_daily_loss_pct,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.post("/paper/accounts/{account_id}/reset")
def reset_paper_account(
    account_id: int, body: PaperAccountResetRequest, db: Session = Depends(get_db)
):
    account = db.get(PaperAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Paper account not found")
    active = db.scalar(select(func.count(PaperPosition.id)).where(
        PaperPosition.account_id == account_id,
        PaperPosition.status.in_(["waiting_entry", "pending", "open", "partially_closed"]),
    )) or 0
    if active:
        raise HTTPException(status_code=409, detail="Close or cancel active paper positions before reset")
    account.starting_balance = account.balance = account.equity = account.max_equity = body.starting_balance
    account.realized_pnl = account.unrealized_pnl = account.drawdown_pct = 0
    db.commit()
    db.refresh(account)
    return account


@router.get("/paper/positions")
def paper_positions(account_id: int | None = None, db: Session = Depends(get_db)):
    query = select(PaperPosition)
    if account_id:
        query = query.where(PaperPosition.account_id == account_id)
    return list(db.scalars(query.order_by(PaperPosition.created_at.desc()).limit(500)))


@router.post("/paper/positions/{position_id}/close")
def close_paper_position(
    position_id: int, body: PaperCloseRequest | None = None, db: Session = Depends(get_db)
):
    position = db.get(PaperPosition, position_id)
    if not position:
        raise HTTPException(status_code=404, detail="Paper position not found")
    if position.status not in {"open", "partially_closed"}:
        raise HTTPException(status_code=409, detail="Only open paper positions can be closed")
    latest = db.scalar(select(Candle).where(
        Candle.symbol_id == position.symbol_id, Candle.is_closed.is_(True),
    ).order_by(Candle.close_time.desc()).limit(1))
    if not latest:
        raise HTTPException(status_code=409, detail="No closed market price is available")
    manual_close(db, position, Decimal(latest.close), latest.close_time, body.slippage_bps if body else None)
    db.commit()
    db.refresh(position)
    return position


@router.post("/paper/positions/{position_id}/cancel")
def cancel_paper_position(position_id: int, db: Session = Depends(get_db)):
    position = db.get(PaperPosition, position_id)
    if not position:
        raise HTTPException(status_code=404, detail="Paper position not found")
    if position.status not in {"waiting_entry", "pending"}:
        raise HTTPException(status_code=409, detail="Only waiting paper positions can be cancelled")
    position.status, position.exit_reason = "cancelled", "manual_cancel"
    setup = db.get(TradeSetup, position.trade_setup_id)
    if setup:
        setup.status = "cancelled"
    db.add(BotLog(
        level="INFO", service="paper", event_type="paper_position_closed",
        message=f"Waiting paper position {position.id} cancelled",
        context_json={"paper_position_id": position.id, "trade_setup_id": position.trade_setup_id, "reason": "cancelled"},
    ))
    db.commit()
    db.refresh(position)
    return position


@router.get("/paper/performance")
def paper_performance(account_id: int | None = None, db: Session = Depends(get_db)):
    query = select(PaperPosition).where(PaperPosition.status.in_(["closed", "stopped"]))
    if account_id:
        query = query.where(PaperPosition.account_id == account_id)
    rows = list(db.scalars(query))
    pnls = [Decimal(row.realized_pnl) for row in rows]
    rs = [Decimal(row.realized_r) for row in rows]
    account = db.get(PaperAccount, account_id) if account_id else None
    starting = Decimal(account.starting_balance) if account else Decimal("10000")
    metrics = calculate_metrics(pnls, rs, starting)
    def grouped(key):
        groups = {}
        for position in rows:
            setup = db.get(TradeSetup, position.trade_setup_id)
            symbol = db.get(Symbol, position.symbol_id)
            value = {
                "strategy": setup.strategy, "symbol": symbol.symbol,
                "timeframe": setup.setup_timeframe, "direction": position.direction,
                "elliott_pattern": (db.get(ElliottWaveCount, setup.elliott_wave_count_id).pattern_type if setup.elliott_wave_count_id else "none"),
                "elliott_wave": setup.setup_conditions_json.get("wave", "none"),
                "confidence_bucket": f"{int(setup.confidence_score // 10) * 10}-{int(setup.confidence_score // 10) * 10 + 9}",
            }[key]
            bucket = groups.setdefault(str(value), {"trades": 0, "net_pnl": Decimal("0"), "wins": 0})
            bucket["trades"] += 1
            bucket["net_pnl"] += position.realized_pnl
            bucket["wins"] += int(position.realized_pnl > 0)
        return [{"value": name, **data} for name, data in groups.items()]
    return {
        **metrics["extended"], "wins": metrics["wins"], "losses": metrics["losses"],
        "breakeven": metrics["break_even"], "win_rate": metrics["win_rate"],
        "net_pnl": metrics["net_profit"], "profit_factor": metrics["profit_factor"],
        "expectancy": metrics["expectancy"], "average_r": metrics["average_rr"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "breakdowns": {key: grouped(key) for key in (
            "strategy", "symbol", "timeframe", "direction", "elliott_pattern",
            "elliott_wave", "confidence_bucket",
        )}, "paper_only": True,
    }


@router.get("/elliott-wave/counts", response_model=list[ElliottWaveCountOut])
def elliott_counts(
    symbol: str,
    timeframe: str,
    degree: str | None = Query(None, pattern="^(minor|intermediate|primary)$"),
    direction: str | None = Query(None, pattern="^(bullish|bearish)$"),
    pattern_type: str | None = None,
    wave_status: str | None = Query(
        None,
        alias="status",
        pattern="^(candidate|primary|alternate|confirmed|completed|invalidated)$",
    ),
    minimum_confidence: float = Query(0, ge=0, le=100),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    query = select(ElliottWaveCount).where(
        ElliottWaveCount.symbol_id == symbol_row.id,
        ElliottWaveCount.timeframe == timeframe,
        ElliottWaveCount.confidence_score >= minimum_confidence,
    )
    if degree:
        query = query.where(ElliottWaveCount.degree == degree)
    if direction:
        query = query.where(ElliottWaveCount.direction == direction)
    if pattern_type:
        query = query.where(ElliottWaveCount.pattern_type == pattern_type)
    if wave_status:
        query = query.where(ElliottWaveCount.status == wave_status)
    return list(
        db.scalars(
            query.order_by(
                ElliottWaveCount.detected_at.desc(), ElliottWaveCount.rank
            ).limit(limit)
        ).unique()
    )


@router.get("/elliott-wave/counts/{count_id}", response_model=ElliottWaveCountOut)
def elliott_count_detail(count_id: int, db: Session = Depends(get_db)):
    row = db.get(ElliottWaveCount, count_id)
    if not row:
        raise HTTPException(status_code=404, detail="Elliott Wave count not found")
    return row


@router.get("/elliott-wave/latest", response_model=ElliottWaveCountOut)
def elliott_latest(symbol: str, timeframe: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    validate_timeframe(timeframe)
    row = db.scalar(
        select(ElliottWaveCount)
        .where(
            ElliottWaveCount.symbol_id == symbol_row.id,
            ElliottWaveCount.timeframe == timeframe,
            ElliottWaveCount.status.in_(["primary", "confirmed", "alternate"]),
        )
        .order_by(ElliottWaveCount.rank, ElliottWaveCount.detected_at.desc())
        .limit(1)
    )
    if not row:
        raise HTTPException(
            status_code=404, detail="Elliott Wave count is not available yet"
        )
    return row


@router.post("/elliott-wave/recalculate")
def elliott_recalculate(body: ElliottRecalculateRequest):
    return recalculate_elliott(body.symbol, body.timeframe, body.rebuild)


@router.get("/elliott-wave/context")
def elliott_context(symbol: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    context = {}
    for timeframe in ("4h", "1h", "15m"):
        row = db.scalar(
            select(ElliottWaveCount)
            .where(
                ElliottWaveCount.symbol_id == symbol_row.id,
                ElliottWaveCount.timeframe == timeframe,
                ElliottWaveCount.status == "primary",
            )
            .order_by(ElliottWaveCount.detected_at.desc())
            .limit(1)
        )
        context[timeframe] = (
            None
            if not row
            else {
                "id": row.id,
                "pattern_type": row.pattern_type,
                "direction": row.direction,
                "degree": row.degree,
                "current_wave": row.metadata_json.get("current_wave"),
                "confidence_score": float(row.confidence_score),
                "invalidation_price": str(row.invalidation_price),
                "target_min": str(row.projected_target_min)
                if row.projected_target_min is not None
                else None,
                "target_max": str(row.projected_target_max)
                if row.projected_target_max is not None
                else None,
            }
        )
    return {"symbol": symbol_row.symbol, "timeframes": context}


@router.post("/market-data/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_market_data(body: SyncRequest):
    return await HistoricalSyncService().sync(
        body.symbol, body.timeframe, body.start_time, body.end_time
    )


@router.post("/analysis/backfill", response_model=AnalysisBackfillReport)
async def analysis_backfill(body: AnalysisBackfillRequest):
    return await AnalysisBackfillService().run(
        body.symbol,
        body.timeframe,
        body.start_time,
        body.end_time,
        body.limit,
        body.rebuild,
    )


@router.get("/analysis/backfill/status", response_model=AnalysisBackfillStatusOut)
def analysis_backfill_progress():
    return backfill_status.report()


@router.get("/market-data/status")
def market_data_status():
    return market_stream.status()


@router.get("/logs", response_model=list[BotLogOut])
def logs(
    level: str | None = Query(None, pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    service: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = select(BotLog)
    if level:
        query = query.where(BotLog.level == level)
    if service:
        query = query.where(BotLog.service == service)
    return list(db.scalars(query.order_by(BotLog.created_at.desc()).limit(limit)))


@router.get("/settings", response_model=RuntimeSettings)
def settings_get(db: Session = Depends(get_db)):
    return get_runtime_settings(db)


@router.put("/settings", response_model=RuntimeSettings)
def settings_put(value: RuntimeSettings, db: Session = Depends(get_db)):
    return save_runtime_settings(db, value)


ws_router = APIRouter()


@ws_router.websocket("/ws/market")
async def market_websocket(websocket: WebSocket):
    await broadcaster.connect(websocket)
    try:
        await websocket.send_json(
            {"type": "connection", "data": {"status": "connected"}}
        )
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        await broadcaster.disconnect(websocket)
    except Exception:
        await broadcaster.disconnect(websocket)
