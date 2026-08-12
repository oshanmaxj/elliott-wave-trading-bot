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


class BatchClient:
    def __init__(self, batch_size=1500, empty=False):
        self.batch_size, self.empty, self.calls = batch_size, empty, []

    async def fetch_historical_klines(self, symbol, timeframe, start, end, limit):
        self.calls.append((start, end, limit))
        if self.empty:
            return []
        step = timedelta(milliseconds={"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[timeframe])
        rows, cursor = [], start
        while cursor <= end and len(rows) < min(limit, self.batch_size):
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
    # Backfill ranges are [start, end), so four hourly candles require two
    # two-row pages.
    assert first["processed_batches"] == 2
    assert first["inserted_candles"] == 4
    with session_factory() as db:
        assert db.scalar(select(func.count(Candle.id))) == 4
    calls = len(client.calls)
    second = await service.run("BTCUSDT", "1h", start=start, end=end)
    assert second["inserted_candles"] == 0
    assert len(client.calls) == calls


@pytest.mark.asyncio
async def test_configured_candle_backfill_triggers_incremental_analysis(session_factory, monkeypatch):
    service = HistoricalBackfillService(client=FakeClient(), session_factory=session_factory)
    candle_runs, analysis_runs = [], []

    async def candle_run(symbol, timeframe):
        candle_runs.append((symbol, timeframe))

    class Analysis:
        def __init__(self, session_factory):
            pass

        async def run(self, symbol, timeframe, start_time=None):
            analysis_runs.append((symbol, timeframe, start_time))

    monkeypatch.setattr(service, "run", candle_run)
    monkeypatch.setattr("app.services.analysis_backfill.AnalysisBackfillService", Analysis)
    monkeypatch.setattr("app.services.historical_backfill.get_settings", lambda: type("S", (), {"default_symbols": ["BTCUSDT"], "default_timeframes": ["1m", "5m", "15m", "1h", "4h"], "analyze_historical_candles": True})())
    await service.run_configured()
    assert len(candle_runs) == len(analysis_runs) == 5
    assert [row[1] for row in analysis_runs] == ["1m", "5m", "15m", "1h", "4h"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("hours,minimum_calls", [(24 * 31, 1), (24 * 365, 6)])
async def test_month_and_year_forward_pagination(session_factory, monkeypatch, hours, minimum_calls):
    monkeypatch.setattr("app.services.historical_backfill.log_event", lambda *args, **kwargs: None)
    client, start = BatchClient(), datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = await HistoricalBackfillService(client, session_factory).run("BTCUSDT", "1h", start=start, end=start + timedelta(hours=hours))
    assert client.calls[0][0] == start
    assert len(client.calls) >= minimum_calls
    assert result["coverage"]["coverage_complete"]
    assert all(b[0] > a[0] for a, b in zip(client.calls, client.calls[1:]))


@pytest.mark.asyncio
async def test_partial_final_batch_and_duplicate_upsert(session_factory, monkeypatch):
    monkeypatch.setattr("app.services.historical_backfill.log_event", lambda *args, **kwargs: None)
    client, start = BatchClient(batch_size=2), datetime(2025, 1, 1, tzinfo=timezone.utc)
    service = HistoricalBackfillService(client, session_factory)
    first = await service.run("BTCUSDT", "1h", start=start, end=start + timedelta(hours=5))
    second = await service.run("BTCUSDT", "1h", start=start, end=start + timedelta(hours=5))
    assert first["inserted_candles"] == 5 and first["processed_batches"] == 3
    assert second["inserted_candles"] == 0


@pytest.mark.asyncio
async def test_empty_response_does_not_claim_completion(session_factory, monkeypatch):
    monkeypatch.setattr("app.services.historical_backfill.log_event", lambda *args, **kwargs: None)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match="coverage remains incomplete"):
        await HistoricalBackfillService(BatchClient(empty=True), session_factory).run("BTCUSDT", "1h", start=start, end=start + timedelta(hours=2))
