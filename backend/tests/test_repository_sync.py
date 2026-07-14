from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select

from app.models import Candle
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData
from app.services.historical_sync import HistoricalSyncService


def data(hour, closed=True):
    start=datetime(2025,1,1,tzinfo=timezone.utc)+timedelta(hours=hour)
    return CandleData(open_time=start,close_time=start+timedelta(hours=1)-timedelta(milliseconds=1),open=Decimal('100'),high=Decimal('105'),low=Decimal('95'),close=Decimal('102'),volume=Decimal('10'),quote_volume=Decimal('1000'),trade_count=10,taker_buy_base_volume=Decimal('5'),taker_buy_quote_volume=Decimal('500'),is_closed=closed)


def test_candle_upsert_is_idempotent_and_updates_open(session_factory):
    with session_factory.begin() as db:
        symbol=ensure_symbol(db,'BTCUSDT'); first,created=upsert_candle(db,symbol.id,'1h',data(0,False)); candle_id=first.id
    changed=data(0,True); changed.close=Decimal('104')
    with session_factory.begin() as db:
        row,created_again=upsert_candle(db,1,'1h',changed)
        assert row.id==candle_id and row.close==Decimal('104') and row.is_closed and not created_again
    with session_factory() as db: assert len(list(db.scalars(select(Candle))))==1


class FakeClient:
    async def fetch_paginated(self,*args,**kwargs): return [data(0),data(1),data(2)]


async def test_historical_sync_and_duplicate_execution(session_factory,monkeypatch):
    import app.services.historical_sync as module
    monkeypatch.setattr(module,'SessionLocal',session_factory)
    monkeypatch.setattr(module,'log_event',lambda *a,**k:None)
    service=HistoricalSyncService(FakeClient())
    one=await service.sync('BTCUSDT','1h',datetime(2025,1,1,tzinfo=timezone.utc))
    two=await service.sync('BTCUSDT','1h',datetime(2025,1,1,tzinfo=timezone.utc))
    assert one['created']==3 and two['created']==0 and two['updated']==3


def test_gap_detection(session_factory,monkeypatch):
    import app.services.historical_sync as module
    monkeypatch.setattr(module,'SessionLocal',session_factory)
    with session_factory.begin() as db:
        symbol=ensure_symbol(db,'BTCUSDT');upsert_candle(db,symbol.id,'1h',data(0));upsert_candle(db,symbol.id,'1h',data(2))
    assert len(HistoricalSyncService(FakeClient()).detect_gaps('BTCUSDT','1h'))==1

