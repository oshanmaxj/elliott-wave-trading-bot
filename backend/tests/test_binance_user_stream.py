import asyncio
import hashlib
import hmac
import json
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode
import pytest
from sqlalchemy import select
from app.models import ExecutionFill, ExecutionOrder, LivePosition, Symbol
from app.services import binance_user_stream as module
from app.services.binance_user_stream import BinanceUserStreamService, normalize_event


def settings():
    return SimpleNamespace(
        binance_api_key="public-key",
        binance_api_secret="private-secret",
        binance_recv_window_ms=5000,
        binance_environment="testnet",
    )


class Socket:
    def __init__(self):
        self.request = None
        self.closed = False
        self.wait = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send(self, payload):
        self.request = json.loads(payload)

    async def recv(self):
        return json.dumps(
            {"id": self.request["id"], "status": 200, "result": {"subscriptionId": 0}}
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.wait.wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True
        self.wait.set()


@pytest.mark.asyncio
async def test_service_connects_duplicate_start_is_prevented_and_stops(
    monkeypatch, session_factory
):
    monkeypatch.setattr(module, "log_event", lambda *a, **k: None)
    socket = Socket()
    service = BinanceUserStreamService(
        session_factory=session_factory, connector=lambda *a, **k: socket
    )
    monkeypatch.setattr(service, "_credentials", lambda: settings())
    assert await service.start() is True
    assert await service.start() is False
    for _ in range(50):
        if service.connected:
            break
        await asyncio.sleep(0.01)
    assert service.status()["connected"] is True and service.status()["running"] is True
    assert socket.request["method"] == "userDataStream.subscribe.signature"
    signed = socket.request["params"]
    payload = urlencode(sorted((k, v) for k, v in signed.items() if k != "signature"))
    assert (
        signed["signature"]
        == hmac.new(b"private-secret", payload.encode(), hashlib.sha256).hexdigest()
    )
    serialized = json.dumps(service.status(), default=str)
    assert "private-secret" not in serialized and "signature" not in serialized
    await service.stop()
    assert service.status()["connected"] is False and socket.closed


@pytest.mark.asyncio
async def test_reconnect_and_credentials_restart(monkeypatch, session_factory):
    monkeypatch.setattr(module, "log_event", lambda *a, **k: None)
    sockets = []
    attempts = 0

    def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary failure")
        socket = Socket()
        sockets.append(socket)
        return socket

    async def no_wait(_):
        return None

    service = BinanceUserStreamService(
        session_factory=session_factory, connector=connect, sleeper=no_wait
    )
    monkeypatch.setattr(service, "_credentials", lambda: settings())
    await service.start()
    for _ in range(100):
        if service.connected:
            break
        await asyncio.sleep(0.01)
    assert service.connected and service.reconnect_count == 1 and attempts >= 2
    await service.restart()
    for _ in range(100):
        if service.connected and len(sockets) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(sockets) >= 2 and sockets[0].closed
    await service.stop()


@pytest.mark.asyncio
async def test_missing_credentials_does_not_crash(monkeypatch, session_factory):
    monkeypatch.setattr(module, "log_event", lambda *a, **k: None)
    service = BinanceUserStreamService(session_factory=session_factory)
    await service.start()
    for _ in range(50):
        if not service.running:
            break
        await asyncio.sleep(0.01)
    assert (
        service.running is False and service.last_error == "credentials_not_available"
    )


def test_normalizes_order_and_balance_events():
    order = normalize_event(
        {
            "subscriptionId": 0,
            "event": {
                "e": "executionReport",
                "E": 1700000000000,
                "T": 1700000000001,
                "s": "BTCUSDT",
                "c": "ws-test-1-entry-1",
                "i": 7,
                "S": "BUY",
                "o": "MARKET",
                "x": "TRADE",
                "X": "PARTIALLY_FILLED",
                "z": ".2",
                "Z": "10000",
                "l": ".2",
                "L": "50000",
                "n": ".0001",
                "N": "BTC",
                "t": 9,
            },
        }
    )
    assert (
        order["type"] == "order"
        and order["last_price"] == Decimal("50000")
        and order["trade_id"] == "9"
    )
    balance = normalize_event(
        {
            "event": {
                "e": "outboundAccountPosition",
                "E": 1700000000000,
                "B": [{"a": "USDT", "f": "10.5", "l": "2"}],
            }
        }
    )
    assert balance["balances"][0]["free"] == Decimal("10.5")


@pytest.mark.asyncio
async def test_fill_is_idempotent_and_position_is_updated(monkeypatch, session_factory):
    monkeypatch.setattr(module, "log_event", lambda *a, **k: None)
    with session_factory.begin() as db:
        symbol = Symbol(
            symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", market_type="spot"
        )
        db.add(symbol)
        db.flush()
        db.add(
            ExecutionOrder(
                environment="testnet",
                symbol_id=symbol.id,
                trade_setup_id=99,
                client_order_id="ws-test-99-entry-1",
                side="BUY",
                order_type="MARKET",
                requested_quantity=Decimal(".2"),
                status="NEW",
                execution_state="acknowledged",
            )
        )
    service = BinanceUserStreamService(session_factory=session_factory)
    event = normalize_event(
        {
            "event": {
                "e": "executionReport",
                "E": 1700000000000,
                "T": 1700000000001,
                "s": "BTCUSDT",
                "c": "ws-test-99-entry-1",
                "i": 7,
                "S": "BUY",
                "o": "MARKET",
                "x": "TRADE",
                "X": "FILLED",
                "z": ".2",
                "Z": "10000",
                "l": ".2",
                "L": "50000",
                "n": ".0001",
                "N": "BTC",
                "t": 9,
            }
        }
    )
    await service.process_event(event)
    await service.process_event(event)
    with session_factory() as db:
        order = db.scalar(select(ExecutionOrder))
        fills = list(db.scalars(select(ExecutionFill)))
        position = db.scalar(select(LivePosition))
        assert order.status == "FILLED" and order.average_fill_price == Decimal("50000")
        assert (
            len(fills) == 1
            and position.status == "open"
            and position.remaining_quantity == Decimal("0.1999")
        )


@pytest.mark.asyncio
async def test_balance_cache_and_external_order_are_safe(monkeypatch, session_factory):
    monkeypatch.setattr(module, "log_event", lambda *a, **k: None)
    service = BinanceUserStreamService(session_factory=session_factory)
    await service.process_event(
        normalize_event(
            {
                "event": {
                    "e": "outboundAccountPosition",
                    "E": 1700000000000,
                    "B": [{"a": "USDT", "f": "10", "l": "2"}],
                }
            }
        )
    )
    assert service.balances()[0]["available"] == "10"
    external = normalize_event(
        {
            "event": {
                "e": "executionReport",
                "E": 1700000000000,
                "s": "BTCUSDT",
                "c": "external-order",
                "i": 88,
                "S": "BUY",
                "o": "LIMIT",
                "x": "NEW",
                "X": "NEW",
                "z": "0",
                "Z": "0",
                "l": "0",
                "L": "0",
                "n": "0",
                "t": -1,
            }
        }
    )
    await service.process_event(external)
    with session_factory() as db:
        assert db.scalar(select(ExecutionOrder)) is None
