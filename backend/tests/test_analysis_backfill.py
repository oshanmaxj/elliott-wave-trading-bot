import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.routes import router
from app.models import Alert, AnalysisSnapshot, Candle, FVGZone, MarketStructureEvent, OrderBlock, SwingPoint, TradeSetup
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData
from app.services.analysis_backfill import AnalysisBackfillService, backfill_status


def candle_data(hour: int, *, high=105, low=95, open_=100, close=102, closed=True) -> CandleData:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hour)
    return CandleData(
        open_time=start,
        close_time=start + timedelta(hours=1) - timedelta(milliseconds=1),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("100"),
        quote_volume=Decimal("10000"),
        trade_count=10,
        taker_buy_base_volume=Decimal("50"),
        taker_buy_quote_volume=Decimal("5000"),
        is_closed=closed,
    )


def seed(session_factory, rows, symbol="BTCUSDT", timeframe="1h"):
    with session_factory.begin() as db:
        symbol_row = ensure_symbol(db, symbol)
        return [upsert_candle(db, symbol_row.id, timeframe, row)[0].id for row in rows]


async def test_backfill_is_chronological_and_skips_open_candles(session_factory, monkeypatch):
    seen = []

    async def processor(candle_id, **kwargs):
        seen.append(candle_id)
        assert kwargs["broadcast"] is False
        return {"processed": True, "events": 1}

    ids = seed(session_factory, [candle_data(2), candle_data(0), candle_data(1, closed=False)])
    service = AnalysisBackfillService(session_factory, processor)
    monkeypatch.setattr("app.services.analysis_backfill.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.analysis_backfill.broadcaster.broadcast", lambda *args, **kwargs: asyncio.sleep(0))
    report = await service.run("BTCUSDT", "1h", limit=None)

    assert seen == [ids[1], ids[0]]
    assert report["total_candles"] == report["processed"] == 2
    assert report["skipped"] == report["failed"] == 0


async def test_backfill_is_idempotent_and_creates_snapshots(session_factory, monkeypatch):
    seed(session_factory, [candle_data(i) for i in range(10)])
    monkeypatch.setattr("app.services.analysis_backfill.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.analysis_backfill.broadcaster.broadcast", lambda *args, **kwargs: asyncio.sleep(0))
    service = AnalysisBackfillService(session_factory)

    first = await service.run("BTCUSDT", "1h", limit=None)
    second = await service.run("BTCUSDT", "1h", limit=None)

    with session_factory() as db:
        snapshots = db.scalar(select(func.count(AnalysisSnapshot.id)))
    assert snapshots == 10
    assert first["processed"] == 10
    assert second["processed"] == 0 and second["skipped"] == 10


async def test_rebuild_deletes_only_derived_data_and_never_candles(session_factory, monkeypatch):
    ids = seed(session_factory, [candle_data(i) for i in range(3)])
    with session_factory.begin() as db:
        candle = db.get(Candle, ids[0])
        db.add(AnalysisSnapshot(symbol_id=candle.symbol_id, timeframe="1h", trend="undefined", latest_structure_event=None, active_fvg_count=0, indicator_values_json={}, confidence_score=Decimal("0.5"), generated_at=candle.close_time))

    async def no_op(candle_id, **kwargs):
        return {"processed": True, "events": 0}

    monkeypatch.setattr("app.services.analysis_backfill.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.analysis_backfill.broadcaster.broadcast", lambda *args, **kwargs: asyncio.sleep(0))
    await AnalysisBackfillService(session_factory, no_op).run("BTCUSDT", "1h", limit=None, rebuild=True)

    with session_factory() as db:
        assert db.scalar(select(func.count(Candle.id))) == 3
        assert db.scalar(select(func.count(AnalysisSnapshot.id))) == 0
        assert db.scalar(select(func.count(SwingPoint.id))) == 0
        assert db.scalar(select(func.count(MarketStructureEvent.id))) == 0
        assert db.scalar(select(func.count(FVGZone.id))) == 0


async def test_historical_replay_creates_swings_fvg_structure_and_snapshots(session_factory, monkeypatch):
    prices = [
        (101, 99, 100, 100), (102, 99, 100, 101), (103, 99, 101, 102),
        (110, 100, 102, 105), (106, 98, 104, 103), (105, 97, 103, 102),
        (104, 96, 102, 101), (103, 94, 101, 99), (102, 92, 99, 95),
        (100, 80, 95, 85), (102, 90, 85, 96), (104, 92, 96, 100),
        (106, 94, 100, 104), (109, 100, 104, 108), (114, 106, 108, 112),
        (113, 105, 112, 109), (111, 103, 109, 106), (109, 101, 106, 104),
        (107, 99, 104, 102), (105, 97, 102, 100), (100, 98, 99, 99),
        (111, 99, 100, 110), (112, 105, 108, 109),
    ]
    seed(session_factory, [candle_data(i, high=high, low=low, open_=open_price, close=close) for i, (high, low, open_price, close) in enumerate(prices)])
    monkeypatch.setattr("app.services.analysis_backfill.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.analysis_backfill.broadcaster.broadcast", lambda *args, **kwargs: asyncio.sleep(0))
    await AnalysisBackfillService(session_factory).run("BTCUSDT", "1h", limit=None, rebuild=True)

    with session_factory() as db:
        assert db.scalar(select(func.count(SwingPoint.id))) >= 2
        assert db.scalar(select(func.count(FVGZone.id))) >= 1
        assert db.scalar(select(func.count(MarketStructureEvent.id))) >= 1
        assert db.scalar(select(func.count(OrderBlock.id))) >= 1
        assert db.scalar(select(func.count(TradeSetup.id))) >= 1
        assert db.scalar(select(func.count(Alert.id))) >= 1
        assert db.scalar(select(func.count(AnalysisSnapshot.id))) == len(prices)


async def test_backfill_status_reports_live_progress(session_factory, monkeypatch):
    seed(session_factory, [candle_data(0)])
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_processor(candle_id, **kwargs):
        entered.set()
        await release.wait()
        return {"processed": True, "events": 0}

    monkeypatch.setattr("app.services.analysis_backfill.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.analysis_backfill.broadcaster.broadcast", lambda *args, **kwargs: asyncio.sleep(0))
    task = asyncio.create_task(AnalysisBackfillService(session_factory, slow_processor).run("BTCUSDT", "1h"))
    await entered.wait()
    assert backfill_status.report()["running"] is True
    assert backfill_status.report()["total_candles"] == 1
    release.set()
    await task
    status = backfill_status.report()
    assert status["running"] is False and status["progress_percentage"] == 100
    assert status["last_completed_at"] is not None


async def test_replay_does_not_use_future_swing_points(session_factory, monkeypatch):
    ids = seed(session_factory, [
        candle_data(0, high=110, low=100, close=105),
        candle_data(1, high=105, low=90, close=95),
        candle_data(2, high=115, low=94, close=112),
    ])
    future_detection = datetime(2025, 1, 2, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        candle = db.get(Candle, ids[0])
        db.add(SwingPoint(symbol_id=candle.symbol_id, timeframe="1h", candle_id=ids[0], swing_type="high", price=Decimal("110"), strength=Decimal("1"), confirmation_candles=3, detected_at=future_detection, metadata_json={}))
        db.add(SwingPoint(symbol_id=candle.symbol_id, timeframe="1h", candle_id=ids[1], swing_type="low", price=Decimal("90"), strength=Decimal("1"), confirmation_candles=3, detected_at=future_detection, metadata_json={}))

    monkeypatch.setattr("app.services.analysis_backfill.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.analysis_backfill.broadcaster.broadcast", lambda *args, **kwargs: asyncio.sleep(0))
    await AnalysisBackfillService(session_factory).run("BTCUSDT", "1h", limit=None)

    with session_factory() as db:
        assert db.scalar(select(func.count(MarketStructureEvent.id))) == 0
        latest = db.scalar(select(AnalysisSnapshot).order_by(AnalysisSnapshot.generated_at.desc()))
        assert latest.latest_structure_event is None


def test_backfill_api_validation_and_status(monkeypatch):
    app = FastAPI()
    app.include_router(router)

    class FakeService:
        async def run(self, symbol, timeframe, start_time, end_time, limit, rebuild):
            now = datetime.now(timezone.utc)
            return {"symbol": symbol, "timeframe": timeframe, "total_candles": 0, "processed": 0, "skipped": 0, "failed": 0, "events_generated": 0, "started_at": now, "completed_at": now, "duration_ms": 0}

    monkeypatch.setattr("app.api.routes.AnalysisBackfillService", FakeService)
    client = TestClient(app)
    assert client.post("/api/analysis/backfill", json={"symbol": "DOGEUSDT", "timeframe": "1h"}).status_code == 422
    assert client.post("/api/analysis/backfill", json={"symbol": "BTCUSDT", "timeframe": "1d"}).status_code == 422
    assert client.post("/api/analysis/backfill", json={"symbol": "btcusdt", "timeframe": "1h"}).status_code == 200
    assert client.get("/api/analysis/backfill/status").status_code == 200
