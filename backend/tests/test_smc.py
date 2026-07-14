from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import make_candle
from app.api.routes import router
from app.database.session import get_db
from app.repositories.market import ensure_symbol
from app.smc.engine import detect_liquidity, detect_order_block, multi_timeframe_bias, order_block_mitigation, premium_discount, structure_score


def swing(id_, kind, price, strength="0.8"):
    return SimpleNamespace(id=id_, swing_type=kind, price=Decimal(str(price)), strength=Decimal(str(strength)))


def test_equal_high_and_low_liquidity_detection():
    high = detect_liquidity([swing(1, "high", 100), swing(2, "low", 90), swing(3, "high", 100.05)], .1)
    low = detect_liquidity([swing(1, "low", 90), swing(2, "high", 100), swing(3, "low", 89.95)], .1)
    assert high.type == "BSL" and high.pattern == "EQH"
    assert low.type == "SSL" and low.pattern == "EQL"
    assert detect_liquidity([swing(1, "high", 100), swing(2, "high", 101)], .1) is None


def test_order_block_uses_last_opposing_candle_before_bos():
    candles = [make_candle(0, open_=100, close=102), make_candle(1, open_=103, close=99), make_candle(2, open_=100, close=110)]
    bos = SimpleNamespace(event_type="BOS", direction="bullish")
    block = detect_order_block(candles, bos)
    assert block.direction == "bullish" and block.candle.id == candles[1].id
    state = SimpleNamespace(direction="bullish", top_price=Decimal("105"), bottom_price=Decimal("95"), status="active", mitigation_percent=Decimal("0"))
    assert order_block_mitigation(state, make_candle(3, low=100, close=102)) == ("partially_mitigated", Decimal("50.0"))
    assert order_block_mitigation(state, make_candle(4, low=90, close=94))[0] == "invalidated"


def test_premium_discount_zones_use_latest_confirmed_range():
    zones = premium_discount([swing(1, "high", 120), swing(2, "low", 80)])
    assert zones["equilibrium"] == Decimal("100")
    assert zones["premium"] == {"bottom": Decimal("100"), "top": Decimal("120")}
    assert zones["discount"] == {"bottom": Decimal("80"), "top": Decimal("100")}


def test_structure_score_is_bounded_and_directional():
    bullish_event = SimpleNamespace(event_type="BOS", direction="bullish")
    bearish_event = SimpleNamespace(event_type="BOS", direction="bearish")
    bullish = structure_score("bullish", bullish_event, 2, 2, 2, {"ema20": 120, "ema50": 110, "ema200": 100})
    bearish = structure_score("bearish", bearish_event, 0, 0, 0, {"ema20": 80, "ema50": 90, "ema200": 100})
    assert bullish["score"] >= 85 and bullish["label"] == "Strong Bullish"
    assert bearish["score"] < 30 and bearish["label"] == "Strong Bearish"


def test_multi_timeframe_bias_alignment_and_weighting():
    aligned = multi_timeframe_bias({"4h": "bullish", "1h": "bullish", "15m": "bullish"})
    mixed = multi_timeframe_bias({"4h": "bearish", "1h": "bullish", "15m": "bullish"})
    assert aligned["aligned"] and aligned["label"] == "Strong Bullish Alignment"
    assert not mixed["aligned"] and mixed["label"] == "Neutral / Mixed"


def test_phase_two_api_endpoints_validate_and_return_empty_state(session_factory):
    with session_factory.begin() as db:
        ensure_symbol(db, "BTCUSDT")
    app = FastAPI()
    app.include_router(router)

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    params = {"symbol": "BTCUSDT", "timeframe": "1h"}
    assert client.get("/api/liquidity", params=params).json() == []
    assert client.get("/api/order-blocks", params=params).json() == []
    assert client.get("/api/alerts", params={"symbol": "BTCUSDT"}).json() == []
    bias = client.get("/api/market-bias", params={"symbol": "BTCUSDT"})
    assert bias.status_code == 200 and bias.json()["label"] == "Neutral / Mixed"
    assert client.get("/api/liquidity", params={"symbol": "DOGEUSDT", "timeframe": "1h"}).status_code == 422
