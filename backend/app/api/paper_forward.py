from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user, require_roles
from app.database.session import get_db
from app.models import PaperForwardTrade
from app.trading.paper_forward import backfill, comparison_rows

router = APIRouter(prefix="/api/paper-forward", tags=["paper-forward"], dependencies=[Depends(current_user)])
D = Decimal


class BackfillRequest(BaseModel):
    symbol: str | None = None
    start: datetime
    end: datetime
    apply: bool = False

    @field_validator("start", "end")
    @classmethod
    def utc(cls, value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def filtered_query(symbol=None, strategy=None, timeframe=None, confidence_min=None, confidence_max=None, start=None, end=None):
    query = select(PaperForwardTrade)
    if symbol:
        query = query.where(PaperForwardTrade.symbol == symbol.upper())
    if strategy:
        query = query.where(PaperForwardTrade.strategy == strategy)
    if timeframe:
        query = query.where(PaperForwardTrade.timeframe == timeframe)
    if confidence_min is not None:
        query = query.where(PaperForwardTrade.confidence_score >= confidence_min)
    if confidence_max is not None:
        query = query.where(PaperForwardTrade.confidence_score <= confidence_max)
    if start:
        query = query.where(PaperForwardTrade.opened_at >= start)
    if end:
        query = query.where(PaperForwardTrade.opened_at <= end)
    return query


def args(symbol=None, strategy=None, timeframe=None, confidence_min=None, confidence_max=None, start=None, end=None):
    return symbol, strategy, timeframe, confidence_min, confidence_max, start, end


def _rows(db, values):
    return list(db.scalars(filtered_query(*values).order_by(PaperForwardTrade.opened_at, PaperForwardTrade.id)))


def stats(rows):
    closed = [r for r in rows if r.status == "closed"]
    headline = [r for r in closed if not r.is_ambiguous]
    wins = [r for r in headline if r.realized_pnl > 0]
    losses = [r for r in headline if r.realized_pnl < 0]
    gross_profit = sum((D(r.realized_pnl) for r in closed if r.realized_pnl > 0), D("0"))
    gross_loss = abs(sum((D(r.realized_pnl) for r in closed if r.realized_pnl < 0), D("0")))
    equity = peak = max_dd = D("0")
    consecutive = maximum_consecutive = 0
    for row in closed:
        equity += D(row.realized_r)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if row.realized_pnl < 0:
            consecutive += 1
            maximum_consecutive = max(maximum_consecutive, consecutive)
        else:
            consecutive = 0
    holding = [(r.closed_at - r.opened_at).total_seconds() for r in closed if r.opened_at and r.closed_at]
    net_r = sum((D(r.realized_r) for r in closed), D("0"))
    return {
        "total_trades": len(closed), "wins": len(wins), "losses": len(losses),
        "ambiguous_excluded": len(closed) - len(headline),
        "win_rate": D(len(wins) * 100) / len(headline) if headline else D("0"),
        "net_pnl": sum((D(r.realized_pnl) for r in closed), D("0")), "net_r": net_r,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "expectancy": net_r / len(closed) if closed else D("0"),
        "average_r": net_r / len(closed) if closed else D("0"),
        "max_drawdown_r": max_dd, "consecutive_losses": maximum_consecutive,
        "average_holding_seconds": sum(holding) / len(holding) if holding else 0,
        "open_trades": sum(r.status in {"open", "partially_closed"} for r in rows),
        "waiting_setups": sum(r.status == "waiting_entry" for r in rows),
        "market_data_source": "binance_production_spot_db",
        "ambiguity_policy": "stop_first; ambiguous closed trades excluded from headline wins/losses/win-rate",
    }


def grouped(rows, field, values=None):
    names = values or sorted({str(getattr(r, field)) for r in rows})
    return [{"value": name, **stats([r for r in rows if str(getattr(r, field)) == name])} for name in names]


def confidence_grouped(rows):
    buckets = [(50, 59), (60, 69), (70, 79), (80, 89), (90, 100)]
    return [{"value": f"{low}-{high}", **stats([r for r in rows if low <= D(r.confidence_score) <= high])} for low, high in buckets]


FilterSymbol = Query(None, max_length=32)
FilterStrategy = Query(None, max_length=64)
FilterTimeframe = Query(None, pattern="^(1m|5m|15m|1h|4h)$")
FilterMin = Query(None, ge=0, le=100)
FilterMax = Query(None, ge=0, le=100)


@router.get("/summary")
def summary(symbol: str | None = FilterSymbol, strategy: str | None = FilterStrategy, timeframe: str | None = FilterTimeframe, confidence_min: Decimal | None = FilterMin, confidence_max: Decimal | None = FilterMax, start: datetime | None = None, end: datetime | None = None, db: Session = Depends(get_db)):
    return stats(_rows(db, args(symbol, strategy, timeframe, confidence_min, confidence_max, start, end)))


@router.get("/trades")
def trades(symbol: str | None = FilterSymbol, strategy: str | None = FilterStrategy, timeframe: str | None = FilterTimeframe, confidence_min: Decimal | None = FilterMin, confidence_max: Decimal | None = FilterMax, start: datetime | None = None, end: datetime | None = None, limit: int = Query(500, ge=1, le=2000), db: Session = Depends(get_db)):
    return _rows(db, args(symbol, strategy, timeframe, confidence_min, confidence_max, start, end))[-limit:]


@router.get("/strategies")
def strategies(symbol: str | None = FilterSymbol, strategy: str | None = FilterStrategy, timeframe: str | None = FilterTimeframe, confidence_min: Decimal | None = FilterMin, confidence_max: Decimal | None = FilterMax, start: datetime | None = None, end: datetime | None = None, db: Session = Depends(get_db)):
    rows = _rows(db, args(symbol, strategy, timeframe, confidence_min, confidence_max, start, end))
    return grouped(rows, "strategy")


@router.get("/confidence")
def confidence(symbol: str | None = FilterSymbol, strategy: str | None = FilterStrategy, timeframe: str | None = FilterTimeframe, confidence_min: Decimal | None = FilterMin, confidence_max: Decimal | None = FilterMax, start: datetime | None = None, end: datetime | None = None, db: Session = Depends(get_db)):
    return confidence_grouped(_rows(db, args(symbol, strategy, timeframe, confidence_min, confidence_max, start, end)))


@router.get("/timeframes")
def timeframes(symbol: str | None = FilterSymbol, strategy: str | None = FilterStrategy, timeframe: str | None = FilterTimeframe, confidence_min: Decimal | None = FilterMin, confidence_max: Decimal | None = FilterMax, start: datetime | None = None, end: datetime | None = None, db: Session = Depends(get_db)):
    return grouped(_rows(db, args(symbol, strategy, timeframe, confidence_min, confidence_max, start, end)), "timeframe", ["1m", "5m", "15m", "1h", "4h"])


@router.get("/compare-testnet")
def compare_testnet(symbol: str | None = FilterSymbol, strategy: str | None = FilterStrategy, timeframe: str | None = FilterTimeframe, confidence_min: Decimal | None = FilterMin, confidence_max: Decimal | None = FilterMax, start: datetime | None = None, end: datetime | None = None, db: Session = Depends(get_db)):
    return comparison_rows(db, _rows(db, args(symbol, strategy, timeframe, confidence_min, confidence_max, start, end)))


@router.post("/backfill", dependencies=[Depends(require_roles("admin"))])
def run_backfill(body: BackfillRequest, db: Session = Depends(get_db)):
    if body.end <= body.start:
        from fastapi import HTTPException
        raise HTTPException(422, "end must be after start")
    return backfill(db, body.symbol, body.start, body.end, body.apply)
