from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Candle
from app.schemas.common import CandleData
from app.services.historical_backfill import HistoricalBackfillService


def data(open_time, timeframe="1h"):
    step = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
    return CandleData(
        open_time=open_time, close_time=open_time + step - timedelta(milliseconds=1),
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
        close=Decimal("100.5"), volume=Decimal("10"), quote_volume=Decimal("1000"),
        trade_count=5, taker_buy_base_volume=Decimal("4"),
        taker_buy_quote_volume=Decimal("400"), is_closed=True,
    )


class FakeClient:
    def __init__(self):
        self.calls = []

    async def fetch_historical_klines(self, symbol, timeframe, start, end, limit):
        self.calls.append((start, end, limit))
        step = timedelta(hours=1)
        rows, cursor = [], start
        while cursor <= end and len(rows) < 2:  # force pagination
            rows.append(data(cursor, timeframe))
            cursor += step
        return rows


@pytest.mark.asyncio
async def test_backfill_paginates_and_is_idempotent(session_factory, monkeypatch):
    monkeypatch.setattr("app.services.historical_backfill.log_event", lambda *args, **kwargs: None)
    client = FakeClient()
    service = HistoricalBackfillService(client=client, session_factory=session_factory)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=4)
    first = await service.run("BTCUSDT", "1h", start=start, end=end)
    assert first["processed_batches"] == 3
    assert first["inserted_candles"] == 5
    with session_factory() as db:
        assert db.scalar(select(func.count(Candle.id))) == 5
    calls = len(client.calls)
    second = await service.run("BTCUSDT", "1h", start=start, end=end)
    assert second["inserted_candles"] == 0
    assert len(client.calls) == calls


@pytest.mark.asyncio
async def test_backfill_resumes_oldest_missing_edge(session_factory, monkeypatch):
    monkeypatch.setattr("app.services.historical_backfill.log_event", lambda *args, **kwargs: None)
    client = FakeClient()
    service = HistoricalBackfillService(client=client, session_factory=session_factory)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    await service.run("BTCUSDT", "1h", start=start + timedelta(hours=2), end=start + timedelta(hours=4))
    client.calls.clear()
    await service.run("BTCUSDT", "1h", start=start, end=start + timedelta(hours=4))
    assert client.calls[0][0].replace(tzinfo=timezone.utc) == start
    assert client.calls[0][1].replace(tzinfo=timezone.utc) == start + timedelta(hours=1)
