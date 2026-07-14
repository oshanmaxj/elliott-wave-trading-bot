from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import router
from app.database.session import get_db
from app.models import AnalysisSnapshot, Candle
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData
from app.services.pipeline import process_closed_candle


def candle_data(hour):
    start=datetime(2025,1,1,tzinfo=timezone.utc)+timedelta(hours=hour)
    price=Decimal(100+hour)
    return CandleData(open_time=start,close_time=start+timedelta(hours=1)-timedelta(milliseconds=1),open=price,high=price+2,low=price-2,close=price+1,volume=Decimal('100'),quote_volume=Decimal('10000'),trade_count=5,taker_buy_base_volume=Decimal('50'),taker_buy_quote_volume=Decimal('5000'),is_closed=True)


def test_health_endpoint(session_factory):
    app=FastAPI();app.include_router(router)
    def override():
        with session_factory() as db: yield db
    app.dependency_overrides[get_db]=override
    response=TestClient(app).get('/api/health')
    assert response.status_code==200 and response.json()['status']=='healthy'


async def test_analysis_pipeline_idempotency(session_factory,monkeypatch):
    import app.services.pipeline as pipeline
    monkeypatch.setattr(pipeline,'SessionLocal',session_factory)
    with session_factory.begin() as db:
        symbol=ensure_symbol(db,'BTCUSDT')
        for i in range(8): row,_=upsert_candle(db,symbol.id,'1h',candle_data(i))
        target=row.id
    first=await process_closed_candle(target);second=await process_closed_candle(target)
    with session_factory() as db:
        assert len(list(db.scalars(select(AnalysisSnapshot))))==1
    assert first['processed'] and second['processed'] and second['events']==0

