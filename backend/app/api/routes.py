from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import SUPPORTED_SYMBOLS, SUPPORTED_TIMEFRAMES
from app.database.session import get_db
from app.market_data.binance_ws import market_stream
from app.models import AnalysisSnapshot, BotLog, Candle, FVGZone, MarketStructureEvent, SwingPoint, Symbol
from app.schemas.common import AnalysisOut, BotLogOut, CandleOut, FVGOut, RuntimeSettings, StructureOut, SwingOut, SymbolOut, SyncRequest
from app.services.broadcast import broadcaster
from app.services.historical_sync import HistoricalSyncService
from app.services.settings import get_runtime_settings, save_runtime_settings

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


@router.post("/market-data/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_market_data(body: SyncRequest):
    return await HistoricalSyncService().sync(body.symbol, body.timeframe, body.start_time, body.end_time)


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

