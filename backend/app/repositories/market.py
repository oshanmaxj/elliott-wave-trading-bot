from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import Candle, Symbol
from app.schemas.common import CandleData


def ensure_symbol(db: Session, symbol: str) -> Symbol:
    existing = db.scalar(select(Symbol).where(Symbol.symbol == symbol))
    if existing:
        return existing
    quote = "USDT"
    row = Symbol(exchange="binance", symbol=symbol, base_asset=symbol.removesuffix(quote), quote_asset=quote, market_type="usdt_perpetual")
    db.add(row)
    db.flush()
    return row


def upsert_candle(db: Session, symbol_id: int, timeframe: str, data: CandleData) -> tuple[Candle, bool]:
    row = db.scalar(select(Candle).where(and_(Candle.symbol_id == symbol_id, Candle.timeframe == timeframe, Candle.open_time == data.open_time)))
    created = row is None
    if row is None:
        row = Candle(symbol_id=symbol_id, timeframe=timeframe, **data.model_dump())
        db.add(row)
    elif not row.is_closed or data.is_closed:
        for key, value in data.model_dump().items():
            setattr(row, key, value)
    db.flush()
    return row, created


def candle_range(db: Session, symbol_id: int, timeframe: str, start: datetime | None = None, end: datetime | None = None, limit: int = 1000) -> list[Candle]:
    query = select(Candle).where(Candle.symbol_id == symbol_id, Candle.timeframe == timeframe)
    if start:
        query = query.where(Candle.open_time >= start)
    if end:
        query = query.where(Candle.open_time <= end)
    return list(db.scalars(query.order_by(Candle.open_time.asc()).limit(limit)))

