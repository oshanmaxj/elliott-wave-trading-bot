from decimal import Decimal
import hashlib
import hmac
from types import SimpleNamespace
from app.execution.binance import BinanceSpotClient
from app.execution.filters import (
    floor_quantity_to_step,
    round_price_to_tick,
    validate_notional,
)
from app.execution.service import client_order_id


def settings(**values):
    base = dict(
        binance_environment="testnet",
        binance_testnet_base_url="https://test",
        binance_production_base_url="https://prod",
        binance_api_key="key",
        binance_api_secret="secret",
        binance_recv_window_ms=5000,
    )
    base.update(values)
    return SimpleNamespace(**base)


def test_hmac_signing_is_stable():
    client = BinanceSpotClient(settings())
    params = {"symbol": "BTCUSDT", "timestamp": "123"}
    assert (
        client.sign(params)
        == hmac.new(
            b"secret", b"symbol=BTCUSDT&timestamp=123", hashlib.sha256
        ).hexdigest()
    )


def test_decimal_filters_round_down_without_float():
    assert floor_quantity_to_step(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert round_price_to_tick(Decimal("123.456"), Decimal("0.1")) == Decimal("123.4")
    info = {"filters": [{"filterType": "MIN_NOTIONAL", "minNotional": "10"}]}
    assert validate_notional(Decimal("2"), Decimal("4.9"), info) == [
        "notional_below_minimum"
    ]


def test_client_order_id_is_deterministic():
    assert (
        client_order_id("testnet", 42)
        == client_order_id("testnet", 42)
        == "ws-test-42-entry-1"
    )
