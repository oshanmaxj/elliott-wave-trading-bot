from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES
from app.database.session import get_db
from app.market_data.binance_ws import market_stream
from app.models import Alert, AnalysisSnapshot, BotLog, Candle, FVGZone, LiquidityPool, MarketStructureEvent, OrderBlock, SwingPoint, Symbol
from app.schemas.common import AlertOut, AnalysisBackfillReport, AnalysisBackfillRequest, AnalysisBackfillStatusOut, AnalysisOut, BotLogOut, CandleOut, FVGOut, LiquidityOut, MarketBiasOut, OrderBlockOut, PremiumDiscountOut, RuntimeSettings, StructureOut, StructureScoreOut, SwingOut, SymbolOut, SyncRequest
from app.services.analysis_backfill import AnalysisBackfillService, backfill_status
from app.services.broadcast import broadcaster
from app.services.historical_sync import HistoricalSyncService
from app.services.settings import get_runtime_settings, save_runtime_settings
from app.smc.engine import multi_timeframe_bias, premium_discount, structure_score

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
    return list(db.scalars(select(Symbol).where(Symbol.is_active.is_(True)).order_by(Symbol.symbol)))


@router.get("/candles", response_model=list[CandleOut])
def candles(symbol: str, timeframe: str, start_time: datetime | None = None, end_time: datetime | None = None, limit: int = Query(500, ge=1, le=1500), db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    query = select(Candle).where(Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe)
    if start_time: query = query.where(Candle.open_time >= start_time)
    if end_time: query = query.where(Candle.open_time <= end_time)
    rows = list(db.scalars(query.order_by(Candle.open_time.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/swings", response_model=list[SwingOut])
def swings(symbol: str, timeframe: str, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    rows = list(db.scalars(select(SwingPoint).where(SwingPoint.symbol_id == symbol_row.id, SwingPoint.timeframe == timeframe).order_by(SwingPoint.detected_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/structure", response_model=list[StructureOut])
def structure(symbol: str, timeframe: str, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    rows = list(db.scalars(select(MarketStructureEvent).where(MarketStructureEvent.symbol_id == symbol_row.id, MarketStructureEvent.timeframe == timeframe).order_by(MarketStructureEvent.detected_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/fvg", response_model=list[FVGOut])
def fvg(symbol: str, timeframe: str, direction: str | None = Query(None, pattern="^(bullish|bearish)$"), zone_status: str | None = Query(None, alias="status", pattern="^(active|partially_mitigated|fully_mitigated|invalidated)$"), limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    query = select(FVGZone).where(FVGZone.symbol_id == symbol_row.id, FVGZone.timeframe == timeframe)
    if direction: query = query.where(FVGZone.direction == direction)
    if zone_status: query = query.where(FVGZone.status == zone_status)
    rows = list(db.scalars(query.order_by(FVGZone.detected_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/analysis/latest", response_model=AnalysisOut)
def latest_analysis(symbol: str, timeframe: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    row = db.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.symbol_id == symbol_row.id, AnalysisSnapshot.timeframe == timeframe).order_by(AnalysisSnapshot.generated_at.desc()).limit(1))
    if not row: raise HTTPException(status_code=404, detail="Analysis is not available yet")
    return row


@router.get("/liquidity", response_model=list[LiquidityOut])
def liquidity(symbol: str, timeframe: str, active_only: bool = False, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    query = select(LiquidityPool).where(LiquidityPool.symbol_id == symbol_row.id, LiquidityPool.timeframe == timeframe)
    if active_only:
        query = query.where(LiquidityPool.swept_at.is_(None))
    rows = list(db.scalars(query.order_by(LiquidityPool.detected_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/order-blocks", response_model=list[OrderBlockOut])
def order_blocks(symbol: str, timeframe: str, block_status: str | None = Query(None, alias="status", pattern="^(active|partially_mitigated|fully_mitigated|invalidated)$"), limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    query = select(OrderBlock).where(OrderBlock.symbol_id == symbol_row.id, OrderBlock.timeframe == timeframe)
    if block_status:
        query = query.where(OrderBlock.status == block_status)
    rows = list(db.scalars(query.order_by(OrderBlock.detected_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/premium-discount", response_model=PremiumDiscountOut)
def premium_discount_zones(symbol: str, timeframe: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    swings = list(db.scalars(select(SwingPoint).where(SwingPoint.symbol_id == symbol_row.id, SwingPoint.timeframe == timeframe).order_by(SwingPoint.detected_at, SwingPoint.id)))
    result = premium_discount(swings)
    if not result:
        raise HTTPException(status_code=404, detail="A confirmed swing range is not available yet")
    return result


@router.get("/market-bias", response_model=MarketBiasOut)
def market_bias(symbol: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol)
    trends = {}
    for timeframe in ("4h", "1h", "15m"):
        snapshot = db.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.symbol_id == symbol_row.id, AnalysisSnapshot.timeframe == timeframe).order_by(AnalysisSnapshot.generated_at.desc()).limit(1))
        trends[timeframe] = snapshot.trend if snapshot else "undefined"
    return {"symbol": symbol_row.symbol, **multi_timeframe_bias(trends)}


@router.get("/structure-score", response_model=StructureScoreOut)
def score(symbol: str, timeframe: str, db: Session = Depends(get_db)):
    symbol_row = resolve_symbol(db, symbol); validate_timeframe(timeframe)
    snapshot = db.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.symbol_id == symbol_row.id, AnalysisSnapshot.timeframe == timeframe).order_by(AnalysisSnapshot.generated_at.desc()).limit(1))
    if not snapshot:
        raise HTTPException(status_code=404, detail="Analysis is not available yet")
    latest_event = db.scalar(select(MarketStructureEvent).where(MarketStructureEvent.symbol_id == symbol_row.id, MarketStructureEvent.timeframe == timeframe).order_by(MarketStructureEvent.detected_at.desc()).limit(1))
    liquidity_count = db.scalar(select(func.count(LiquidityPool.id)).where(LiquidityPool.symbol_id == symbol_row.id, LiquidityPool.timeframe == timeframe, LiquidityPool.swept_at.is_(None))) or 0
    block_count = db.scalar(select(func.count(OrderBlock.id)).where(OrderBlock.symbol_id == symbol_row.id, OrderBlock.timeframe == timeframe, OrderBlock.status.in_(["active", "partially_mitigated"]))) or 0
    result = structure_score(snapshot.trend, latest_event, liquidity_count, block_count, snapshot.active_fvg_count, snapshot.indicator_values_json)
    return {"symbol": symbol_row.symbol, "timeframe": timeframe, **result}


@router.get("/alerts", response_model=list[AlertOut])
def alerts(symbol: str | None = None, timeframe: str | None = None, limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    query = select(Alert)
    if symbol:
        symbol_row = resolve_symbol(db, symbol)
        query = query.where(Alert.symbol_id == symbol_row.id)
    if timeframe:
        query = query.where(Alert.timeframe == validate_timeframe(timeframe))
    return list(db.scalars(query.order_by(Alert.created_at.desc()).limit(limit)))


@router.post("/market-data/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_market_data(body: SyncRequest):
    return await HistoricalSyncService().sync(body.symbol, body.timeframe, body.start_time, body.end_time)


@router.post("/analysis/backfill", response_model=AnalysisBackfillReport)
async def analysis_backfill(body: AnalysisBackfillRequest):
    return await AnalysisBackfillService().run(
        body.symbol, body.timeframe, body.start_time, body.end_time, body.limit, body.rebuild
    )


@router.get("/analysis/backfill/status", response_model=AnalysisBackfillStatusOut)
def analysis_backfill_progress():
    return backfill_status.report()


@router.get("/market-data/status")
def market_data_status():
    return market_stream.status()


@router.get("/logs", response_model=list[BotLogOut])
def logs(level: str | None = Query(None, pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"), service: str | None = None, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    query = select(BotLog)
    if level: query = query.where(BotLog.level == level)
    if service: query = query.where(BotLog.service == service)
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
        await websocket.send_json({"type": "connection", "data": {"status": "connected"}})
        while True:
            message = await websocket.receive_text()
            if message == "ping": await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        await broadcaster.disconnect(websocket)
    except Exception:
        await broadcaster.disconnect(websocket)
