import asyncio
from datetime import datetime, timezone

import pytest

from app.market_data.binance_rest import milliseconds
from app.market_data.binance_ws import BinanceWebSocketManager
from app.schemas.common import BacktestRequest


def test_naive_and_offset_dates_normalize_to_utc():
    naive = BacktestRequest(symbol="BTCUSDT", timeframe="1h", start_time="2025-01-01T00:00:00", end_time="2025-01-02T00:00:00")
    offset = BacktestRequest(symbol="BTCUSDT", timeframe="1h", start_time="2025-01-01T05:30:00+05:30", end_time="2025-01-02T05:30:00+05:30")
    assert naive.start_time == offset.start_time == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert milliseconds(naive.start_time) == 1735689600000


@pytest.mark.asyncio
async def test_websocket_reconnect_logs_reason_and_backoff(monkeypatch):
    manager = BinanceWebSocketManager()
    calls, sleeps = [], []

    class FailedConnection:
        async def __aenter__(self):
            raise ConnectionError("test disconnect")
        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.market_data.binance_ws.websockets.connect", lambda *args, **kwargs: FailedConnection())
    monkeypatch.setattr("app.market_data.binance_ws.log_event", lambda *args, **kwargs: calls.append((args, kwargs)))

    async def stop_after_delay(delay):
        sleeps.append(delay); manager.running = False
    monkeypatch.setattr(asyncio, "sleep", stop_after_delay)
    await manager.run()
    context = calls[-1][0][4]
    assert context["exception_type"] == "ConnectionError"
    assert context["exception_message"] == "test disconnect"
    assert context["reconnect_attempt"] == 1 and sleeps == [1]


@pytest.mark.asyncio
async def test_duplicate_websocket_manager_is_not_started(monkeypatch):
    manager, events = BinanceWebSocketManager(), []
    manager.running = True
    monkeypatch.setattr("app.market_data.binance_ws.log_event", lambda *args, **kwargs: events.append(args))
    await manager.run()
    assert events[0][2] == "duplicate_manager_ignored"
