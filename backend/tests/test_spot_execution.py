from decimal import Decimal
import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs
import httpx
import pytest
from app.execution.binance import BinanceSpotClient
from app.execution.filters import (
    floor_quantity_to_step,
    quantity_limits,
    round_price_to_tick,
    serialize_quantity,
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


@pytest.mark.parametrize(
    "quantity,step,expected",
    [
        (Decimal("0.530300000000"), Decimal("0.00001000"), "0.5303"),
        (Decimal("0.015700000000"), Decimal("0.00001000"), "0.0157"),
        (Decimal("0.527900000000"), Decimal("0.00001000"), "0.5279"),
        (Decimal("0.000010000000"), Decimal("0.00001000"), "0.00001"),
        (Decimal("1.23456789"), Decimal("0.00001000"), "1.23456"),
        (Decimal("12.345600000"), Decimal("0.00100000"), "12.345"),
        (Decimal("0.00700000"), Decimal("0.00010000"), "0.007"),
        (Decimal("10.00000000"), Decimal("0.00001000"), "10"),
        (Decimal("100.00000000"), Decimal("1.00000000"), "100"),
    ],
)
def test_quantity_serialization_uses_decimal_step_without_excess_precision(
    quantity, step, expected
):
    assert serialize_quantity(quantity, step) == expected


@pytest.mark.asyncio
async def test_binance_client_sends_pre_normalized_quantity_string_unchanged():
    captured = {}

    def handler(request):
        captured.update(parse_qs(request.url.query.decode()))
        return httpx.Response(200, json={"orderId": 1, "status": "NEW"})

    client = BinanceSpotClient(
        settings(), transport=httpx.MockTransport(handler)
    )
    try:
        quantity = serialize_quantity(
            Decimal("0.530300000000"), Decimal("0.00001000")
        )
        await client.place_order(
            {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": quantity}
        )
    finally:
        await client.close()
    assert captured["quantity"] == ["0.5303"]


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
