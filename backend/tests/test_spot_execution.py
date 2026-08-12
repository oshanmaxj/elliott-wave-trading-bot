from decimal import Decimal
import hashlib
import hmac
from types import SimpleNamespace
from app.execution.binance import BinanceSpotClient
from app.execution.filters import (
    floor_quantity_to_step,
    quantity_limits,
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


def filters(market=None, lot=None):
    rows = []
    if market is not None:
        rows.append({"filterType": "MARKET_LOT_SIZE", **market})
    if lot is not None:
        rows.append({"filterType": "LOT_SIZE", **lot})
    return {"filters": rows}


def test_market_zero_step_falls_back_to_lot_size_step():
    info = filters(
        {"minQty": "0", "maxQty": "100", "stepSize": "0"},
        {"minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
    )
    assert quantity_limits(info) == (
        Decimal("0.00001"), Decimal("100"), Decimal("0.00001")
    )


def test_positive_market_step_takes_precedence_over_lot_step():
    info = filters(
        {"minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
        {"minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
    )
    assert quantity_limits(info)[2] == Decimal("0.001")


def test_missing_market_filter_uses_lot_size():
    info = filters(
        lot={"minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"}
    )
    assert quantity_limits(info) == (
        Decimal("0.00001"), Decimal("9000"), Decimal("0.00001")
    )


def test_btcusdt_testnet_market_limits_combine_with_lot_step_and_minimum():
    info = filters(
        {"minQty": "0.00000000", "maxQty": "141.67845966", "stepSize": "0.00000000"},
        {"minQty": "0.00001000", "maxQty": "9000.00000000", "stepSize": "0.00001000"},
    )
    minimum, maximum, step = quantity_limits(info)
    assert (minimum, maximum, step) == (
        Decimal("0.00001000"),
        Decimal("141.67845966"),
        Decimal("0.00001000"),
    )
    assert floor_quantity_to_step(min(Decimal("141.67846999"), maximum), step) <= maximum
