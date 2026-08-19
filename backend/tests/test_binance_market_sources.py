from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.market_data.binance_rest import BinanceRESTClient


def test_default_market_data_source_is_spot_production():
    settings = Settings(_env_file=None)
    assert settings.binance_rest_base_url == "https://api.binance.com"
    assert settings.binance_ws_base_url == "wss://stream.binance.com:9443/stream"


def test_spot_client_rejects_futures_rest_domain():
    with pytest.raises(ValueError, match="cannot use Futures host"):
        BinanceRESTClient("https://fapi.binance.com")


@pytest.mark.parametrize(
    "rest_url,ws_url",
    [
        ("https://fapi.binance.com", "wss://stream.binance.com:9443/stream"),
        ("https://api.binance.com", "wss://fstream.binance.com/stream"),
        ("https://api.binance.com", "wss://stream.testnet.binance.vision/ws"),
    ],
)
def test_settings_reject_futures_or_mixed_market_sources(rest_url, ws_url):
    with pytest.raises((ValueError, ValidationError)):
        Settings(
            _env_file=None,
            binance_rest_base_url=rest_url,
            binance_ws_base_url=ws_url,
        )


@pytest.mark.asyncio
async def test_historical_backfill_constructs_spot_kline_url(monkeypatch):
    request = None

    async def handler(value: httpx.Request):
        nonlocal request
        request = value
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    await BinanceRESTClient("https://api.binance.com").fetch_historical_klines(
        "BTCUSDT", "1h"
    )
    assert request is not None
    assert str(request.url).startswith(
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h"
    )


def test_btcusdt_spot_kline_parsing():
    row = [
        1_766_016_000_000,
        "87000.10",
        "87500.20",
        "86500.30",
        "87250.40",
        "123.45",
        1_766_019_599_999,
        "10700000.50",
        9876,
        "61.25",
        "5320000.75",
        "0",
    ]
    candle = BinanceRESTClient.normalize_kline(
        row, now=datetime(2030, 1, 1, tzinfo=timezone.utc)
    )
    assert candle.open == Decimal("87000.10")
    assert candle.high == Decimal("87500.20")
    assert candle.low == Decimal("86500.30")
    assert candle.close == Decimal("87250.40")
    assert candle.is_closed
