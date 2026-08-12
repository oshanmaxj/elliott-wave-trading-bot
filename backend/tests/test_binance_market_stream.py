import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.market_data.binance_ws import BinanceWebSocketManager
from app.models import Candle


def kline_message(timeframe="1m", closed=False, close="101", nested=True):
    event = {
        "e": "kline",
        "E": 1767225660000,
        "s": "BTCUSDT",
        "k": {
            "t": 1767225600000,
            "T": 1767225659999 if timeframe == "1m" else 1767225899999,
            "s": "BTCUSDT",
            "i": timeframe,
            "o": "100",
            "c": close,
            "h": "102",
            "l": "99",
            "v": "10",
            "n": 12,
            "x": closed,
            "q": "1000",
            "V": "5",
            "Q": "500",
        },
    }
    payload = {"stream": f"btcusdt@kline_{timeframe}", "data": event} if nested else event
    return json.dumps(payload)


@pytest.fixture
def manager(session_factory, monkeypatch):
    processed = []

    async def processor(candle_id):
        processed.append(candle_id)

    instance = BinanceWebSocketManager(session_factory, processor)
    monkeypatch.setattr("app.market_data.binance_ws.broadcaster.broadcast", lambda *args: _done())
    instance.processed = processed
    return instance


async def _done():
    return None


@pytest.mark.asyncio
async def test_combined_open_then_closed_one_minute_updates_and_processes_once(manager, session_factory):
    await manager.handle_message(kline_message(closed=False, close="100.5"))
    assert manager.messages_received == manager.kline_messages_received == 1
    assert manager.last_kline_at and manager.last_closed_candle_at is None
    with session_factory() as db:
        forming = db.scalar(select(Candle))
        assert forming.close == Decimal("100.5") and not forming.is_closed

    closed = kline_message(closed=True)
    await manager.handle_message(closed)
    await manager.handle_message(closed)
    assert manager.messages_received == manager.kline_messages_received == 3
    assert manager.last_closed_candle_at is not None
    assert len(manager.processed) == 1
    with session_factory() as db:
        rows = list(db.scalars(select(Candle)))
        assert len(rows) == 1 and rows[0].is_closed and rows[0].close == Decimal("101")


@pytest.mark.asyncio
async def test_nested_closed_five_minute_and_root_payload_are_recognized(manager):
    await manager.handle_message(kline_message("5m", True, nested=True))
    await manager.handle_message(kline_message("1m", True, nested=False))
    assert manager.kline_messages_received == 2
    assert len(manager.processed) == 2


@pytest.mark.asyncio
async def test_malformed_and_unrelated_messages_are_ignored_without_reconnect(manager):
    await manager.handle_message("{broken")
    await manager.handle_message(json.dumps({"stream": "btcusdt@trade", "data": {"e": "trade"}}))
    await manager.handle_message(kline_message(closed=False))
    assert manager.messages_received == 3
    assert manager.kline_messages_received == 1
    assert manager.reconnect_count == 0


@pytest.mark.asyncio
async def test_analysis_exception_does_not_stop_subsequent_market_messages(session_factory, monkeypatch):
    calls = []

    async def failing_processor(candle_id):
        calls.append(candle_id)
        raise RuntimeError("safe test failure")

    monkeypatch.setattr("app.market_data.binance_ws.broadcaster.broadcast", lambda *args: _done())
    manager = BinanceWebSocketManager(session_factory, failing_processor)
    await manager.handle_message(kline_message("1m", True))
    await manager.handle_message(kline_message("5m", True))
    assert len(calls) == 2
    assert manager.kline_messages_received == 2
    assert manager.reconnect_count == 0


def test_spot_combined_stream_url_is_normalized(manager):
    manager.settings.binance_ws_base_url = "wss://stream.testnet.binance.vision/ws"
    assert manager.combined_stream_url.startswith("wss://stream.testnet.binance.vision/stream?streams=")
    assert "btcusdt@kline_1m" in manager.combined_stream_url
