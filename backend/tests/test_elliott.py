from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.routes import router
from app.database.session import get_db
from app.elliott.engine import (
    assign_degree,
    generate_candidates,
    validate_impulse,
    validate_zigzag,
)
from app.elliott.service import process_elliott_candidates, recalculate_elliott
from app.elliott.setups import select_wave_strategy
from app.models import ElliottWaveCount, SwingPoint
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData, RuntimeSettings


def wave_swing(index, kind, price, strength=0.8):
    opened = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    candle = SimpleNamespace(
        id=index + 1,
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        high=Decimal(str(price + 1)),
        low=Decimal(str(price - 1)),
    )
    return SimpleNamespace(
        id=index + 1,
        swing_type=kind,
        price=Decimal(str(price)),
        strength=Decimal(str(strength)),
        candle=candle,
        detected_at=candle.close_time,
    )


def sequence(prices, bullish=True):
    return [
        wave_swing(
            i,
            ("low" if i % 2 == 0 else "high")
            if bullish
            else ("high" if i % 2 == 0 else "low"),
            price,
        )
        for i, price in enumerate(prices)
    ]


def test_valid_bullish_and_bearish_impulses_with_fibonacci_measurements():
    bull = validate_impulse(sequence([100, 110, 105, 125, 115, 130]), "bullish")
    bear = validate_impulse(sequence([130, 120, 125, 105, 115, 100], False), "bearish")
    assert bull.valid and bear.valid
    assert set(bull.fibonacci) == {
        "wave_2_retracement",
        "wave_3_extension",
        "wave_4_retracement",
        "wave_5_vs_wave_1",
        "wave_5_vs_waves_1_through_3",
    }
    assert bull.fibonacci["wave_2_retracement"]["actual"] == 0.5
    aligned = validate_impulse(
        sequence([100, 110, 105]), "bullish", context={"higher_timeframe": "bullish"}
    )
    conflicting = validate_impulse(
        sequence([100, 110, 105]), "bullish", context={"higher_timeframe": "bearish"}
    )
    assert aligned.confidence_score - conflicting.confidence_score == 20


def test_impulse_hard_invalidations_override_score():
    invalid_w2 = validate_impulse(
        sequence([100, 110, 99, 125, 115, 130]),
        "bullish",
        context={
            "structure": True,
            "liquidity": True,
            "zone": True,
            "momentum": True,
            "higher_timeframe": True,
        },
    )
    shortest_w3 = validate_impulse(sequence([100, 110, 105, 111, 108, 125]), "bullish")
    overlap_w4 = validate_impulse(sequence([100, 110, 105, 125, 109, 130]), "bullish")
    assert (
        "wave_2_does_not_exceed_origin" in invalid_w2.rules_failed
        and invalid_w2.confidence_score == 0
    )
    assert "wave_3_not_shortest" in shortest_w3.rules_failed
    assert "wave_4_no_wave_1_overlap" in overlap_w4.rules_failed


def test_valid_bullish_bearish_zigzags_and_truncation_control():
    bullish = validate_zigzag(sequence([100, 110, 105, 116]), "bullish")
    bearish = validate_zigzag(sequence([120, 110, 115, 104], False), "bearish")
    truncated = validate_zigzag(sequence([100, 110, 105, 109]), "bullish")
    allowed = validate_zigzag(
        sequence([100, 110, 105, 109]), "bullish", allow_truncation=True
    )
    assert bullish.valid and bearish.valid and not truncated.valid and allowed.valid


def test_bullish_and_bearish_wave_3_setup_confirmations():
    confirmations = {"CHoCH", "BOS"}
    assert select_wave_strategy("2", "bullish", True, confirmations) == "bullish_wave_3"
    assert select_wave_strategy("2", "bearish", True, confirmations) == "bearish_wave_3"
    assert select_wave_strategy("2", "bullish", False, confirmations) is None
    assert select_wave_strategy("2", "bullish", True, {"CHoCH"}) is None


def test_multiple_candidates_and_degree_assignment():
    swings = sequence([100, 110, 105, 125, 115, 130, 120, 140])
    candidates = generate_candidates(swings, context={"structure": True, "zone": True})
    assert len(candidates) >= 3 and {item.pattern_type for item in candidates} >= {
        "bullish_impulse",
        "bullish_zigzag",
    }
    assert assign_degree(
        "15m",
        [
            wave_swing(i, "low" if i % 2 == 0 else "high", price, 0.9)
            for i, price in enumerate([100, 110, 105])
        ],
    ) in {"minor", "intermediate"}
    strong = sequence([100, 110, 105])
    strong[-1].candle.open_time = strong[0].candle.open_time + timedelta(days=3)
    assert assign_degree("1h", strong) == "primary"


def candle_data(index, price):
    opened = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    value = Decimal(str(price))
    return CandleData(
        open_time=opened,
        close_time=opened + timedelta(hours=1) - timedelta(milliseconds=1),
        open=value,
        high=value + 1,
        low=value - 1,
        close=value,
        volume=100,
        quote_volume=10000,
        trade_count=10,
        taker_buy_base_volume=50,
        taker_buy_quote_volume=5000,
        is_closed=True,
    )


def seed_wave(session_factory):
    prices = [100, 110, 105, 125, 115, 130, 120, 140]
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, "BTCUSDT")
        rows = []
        for i, price in enumerate(prices):
            candle, _ = upsert_candle(db, symbol.id, "1h", candle_data(i, price))
            rows.append(candle)
            db.add(
                SwingPoint(
                    symbol_id=symbol.id,
                    timeframe="1h",
                    candle_id=candle.id,
                    swing_type="low" if i % 2 == 0 else "high",
                    price=price,
                    strength=Decimal(".9"),
                    confirmation_candles=1,
                    detected_at=candle.close_time,
                    metadata_json={},
                )
            )
    return rows


def test_persistence_ranking_duplicate_prevention_and_alternate_promotion(
    session_factory,
):
    rows = seed_wave(session_factory)
    with session_factory.begin() as db:
        swings = list(db.scalars(select(SwingPoint).order_by(SwingPoint.detected_at)))
        changed, primary = process_elliott_candidates(
            db,
            rows[-1],
            swings,
            RuntimeSettings(elliott_minimum_confidence=35),
            {
                "structure": True,
                "liquidity": True,
                "zone": True,
                "higher_timeframe": True,
            },
        )
        assert primary and primary.status == "primary"
        original = primary.id
        first_count = db.scalar(select(func.count(ElliottWaveCount.id)))
        process_elliott_candidates(
            db, rows[-1], swings, RuntimeSettings(elliott_minimum_confidence=35), {}
        )
        assert db.scalar(select(func.count(ElliottWaveCount.id))) == first_count
        primary.invalidation_price = Decimal(
            "141" if primary.direction == "bullish" else "129"
        )
    with session_factory.begin() as db:
        swings = list(db.scalars(select(SwingPoint).order_by(SwingPoint.detected_at)))
        _, promoted = process_elliott_candidates(
            db, rows[-1], swings, RuntimeSettings(elliott_minimum_confidence=35), {}
        )
        assert db.get(ElliottWaveCount, original).status == "invalidated"
        assert promoted and promoted.id != original and promoted.status == "primary"


def test_historical_recalculation_is_deterministic_and_causal(session_factory):
    seed_wave(session_factory)
    first = recalculate_elliott("BTCUSDT", "1h", True, session_factory)
    second = recalculate_elliott("BTCUSDT", "1h", True, session_factory)
    assert first["processed"] == second["processed"] == 8
    assert first["counts"] == second["counts"] > 0
    with session_factory() as db:
        for count in db.scalars(select(ElliottWaveCount)):
            assert all(point.timestamp <= count.detected_at for point in count.points)


def test_elliott_api_and_no_lookahead_timestamps(session_factory):
    rows = seed_wave(session_factory)
    with session_factory.begin() as db:
        swings = list(db.scalars(select(SwingPoint).order_by(SwingPoint.detected_at)))
        process_elliott_candidates(
            db,
            rows[-1],
            swings,
            RuntimeSettings(elliott_minimum_confidence=35),
            {"structure": True, "zone": True},
        )
        count = db.scalar(
            select(ElliottWaveCount).where(ElliottWaveCount.status == "primary")
        )
        assert count.detected_at >= max(
            point.detected_at for point in swings[: count.metadata_json["point_count"]]
        )
    app = FastAPI()
    app.include_router(router)

    def override():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    client = TestClient(app)
    response = client.get(
        "/api/elliott-wave/counts", params={"symbol": "BTCUSDT", "timeframe": "1h"}
    )
    assert response.status_code == 200 and response.json()[0]["points"]
    assert (
        client.get(
            "/api/elliott-wave/latest", params={"symbol": "BTCUSDT", "timeframe": "1h"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/elliott-wave/context", params={"symbol": "BTCUSDT"}
        ).status_code
        == 200
    )
