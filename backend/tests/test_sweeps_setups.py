from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import LiquidityPool, LiquiditySweep
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import RuntimeSettings
from app.smc.setups import generate_setup, update_setup_lifecycle
from app.smc.sweeps import detect_sweep, update_sweep
from conftest import make_candle
from test_repository_sync import data


def settings(**changes):
    return RuntimeSettings(**changes)


def pool(kind="SSL", price=100, strength=1):
    return SimpleNamespace(id=1, type=kind, price=Decimal(str(price)), strength=Decimal(str(strength)), status="active")


def candle(index, **changes):
    row = make_candle(index, **changes)
    row.timeframe = "15m"
    return row


def test_bullish_ssl_and_bearish_bsl_same_candle_sweeps():
    bullish = detect_sweep(pool("SSL"), candle(0, high=101, low=99.2, open_=100.2, close=100.6), settings(), 1.5, True, True)
    bearish = detect_sweep(pool("BSL"), candle(0, high=100.8, low=99, open_=99.8, close=99.4), settings(), 1.5, True, True)
    assert bullish.status == "confirmed" and bullish.sweep_type == "sell_side_sweep" and bullish.direction == "bullish"
    assert bearish.status == "confirmed" and bearish.sweep_type == "buy_side_sweep" and bearish.direction == "bearish"
    assert 70 <= bullish.confidence_score <= 100


def test_multi_candle_reclaim_and_failed_sweep():
    config = settings(sweep_allow_same_candle_confirmation=False)
    first = detect_sweep(pool(), candle(0, high=100.2, low=99.2, open_=100, close=99.6), config, 1.2, True)
    candidate = SimpleNamespace(status="candidate", direction="bullish", sweep_type=first.sweep_type, extreme_price=first.extreme_price, penetration_percentage=first.penetration_percentage, confidence_score=first.confidence_score, metadata_json={"score_breakdown": first.score_breakdown})
    confirmed = update_sweep(candidate, pool(), candle(1, high=101, low=99.8, open_=99.8, close=100.7), 1, config, 1.5, True, True)
    failed = update_sweep(candidate, pool(), candle(4, high=100, low=98.9, open_=99.5, close=99), 3, config)
    assert confirmed.status == "confirmed" and confirmed.reclaimed_price == Decimal("100.7")
    assert failed.status == "invalidated" and failed.sweep_type == "failed_sweep"


def test_excessive_penetration_hard_invalidates_sweep():
    decision = detect_sweep(pool(), candle(0, high=101, low=95, open_=100, close=100.5), settings(sweep_maximum_penetration_percentage=1), 2, True, True)
    assert decision.status == "invalidated" and decision.confidence_score == 0


def setup_inputs(direction="bullish", **setting_changes):
    config = settings(**setting_changes)
    current = candle(10, high=105, low=98, open_=100, close=103)
    event = SimpleNamespace(id=4, direction=direction, event_type="CHoCH", detected_at=current.close_time)
    sweep = SimpleNamespace(id=3, status="confirmed", direction=direction, extreme_price=Decimal("95" if direction == "bullish" else "105"), confidence_score=Decimal("90"), confirmed_at=current.close_time-timedelta(minutes=15))
    fvg = SimpleNamespace(id=5, direction=direction, status="active", lower_price=Decimal("99"), upper_price=Decimal("101"))
    block = SimpleNamespace(id=6, direction=direction, status="active", bottom_price=Decimal("98"), top_price=Decimal("102"))
    swing_kind = "high" if direction == "bullish" else "low"
    target_price = Decimal("115" if direction == "bullish" else "85")
    swings = [SimpleNamespace(swing_type=swing_kind, price=target_price)]
    target_pool = SimpleNamespace(type="BSL" if direction == "bullish" else "SSL", price=Decimal("120" if direction == "bullish" else "80"), status="active")
    indicators = {"atr14": 2, "ema20": 105 if direction == "bullish" else 95, "ema50": 100}
    premium = {"equilibrium": Decimal("105" if direction == "bullish" else "95")}
    htf = direction
    return config, current, event, sweep, [fvg], [block], swings, [target_pool], indicators, htf, premium


@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_liquidity_reversal_setup_has_valid_entry_stop_targets(direction):
    args = setup_inputs(direction)
    result = generate_setup(direction, args[2], args[1], args[0], *args[4:9], args[9], args[10], args[3])
    assert result.strategy == f"{direction}_liquidity_reversal" and result.status == "ready"
    assert result.entry_min < result.entry_max and result.risk_rewards[1] >= Decimal("1.5")
    assert (result.stop_loss < result.preferred_entry) if direction == "bullish" else (result.stop_loss > result.preferred_entry)


def test_continuation_and_hard_setup_rejections():
    args = setup_inputs("bullish")
    continuation = generate_setup("bullish", args[2], args[1], args[0], *args[4:9], args[9], args[10], None, True)
    no_zone = generate_setup("bullish", args[2], args[1], args[0], [], [], *args[6:9], args[9], args[10], args[3])
    high_rr_settings = settings(minimum_reward_to_risk=5)
    rr_rejected = generate_setup("bullish", args[2], args[1], high_rr_settings, *args[4:9], args[9], args[10], args[3])
    assert continuation.strategy == "bullish_continuation" and continuation.status == "ready"
    assert "no valid entry zone" in no_zone.rejection_reasons
    assert "reward-to-risk below threshold" in rr_rejected.rejection_reasons
    bad_sweep = SimpleNamespace(**{**args[3].__dict__, "extreme_price": Decimal("103")})
    wrong_stop = generate_setup("bullish", args[2], args[1], args[0], *args[4:9], args[9], args[10], bad_sweep)
    countertrend = generate_setup("bullish", args[2], args[1], args[0], *args[4:9], "bearish", args[10], args[3])
    assert "stop loss on the wrong side" in wrong_stop.rejection_reasons
    assert "higher-timeframe counter-trend setup disabled" in countertrend.rejection_reasons


def test_setup_trigger_invalidation_and_expiry_lifecycle():
    now = candle(10).close_time
    base = dict(status="ready", direction="bullish", entry_min=Decimal("99"), entry_max=Decimal("101"), invalidation_price=Decimal("95"), expires_at=now+timedelta(hours=1))
    assert update_setup_lifecycle(SimpleNamespace(**base), candle(11, high=102, low=100, close=101)) == "triggered"
    assert update_setup_lifecycle(SimpleNamespace(**base), candle(11, high=100, low=94, close=96)) == "invalidated"
    expired = SimpleNamespace(**{**base, "expires_at": now-timedelta(hours=1)})
    assert update_setup_lifecycle(expired, candle(11)) == "expired"


def test_duplicate_sweep_constraint(session_factory):
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, "BTCUSDT")
        first, _ = upsert_candle(db, symbol.id, "1h", data(0))
        second, _ = upsert_candle(db, symbol.id, "1h", data(1))
        from app.models import SwingPoint
        one = SwingPoint(symbol_id=symbol.id, timeframe="1h", candle_id=first.id, swing_type="low", price=Decimal("100"), strength=Decimal("1"), confirmation_candles=1, detected_at=first.close_time, metadata_json={})
        two = SwingPoint(symbol_id=symbol.id, timeframe="1h", candle_id=second.id, swing_type="low", price=Decimal("100"), strength=Decimal("1"), confirmation_candles=1, detected_at=second.close_time, metadata_json={})
        db.add_all([one, two])
        db.flush()
        liquidity = LiquidityPool(symbol_id=symbol.id, timeframe="1h", type="SSL", price=Decimal("100"), strength=Decimal("1"), first_swing_id=one.id, second_swing_id=two.id, detected_at=second.close_time, metadata_json={})
        db.add(liquidity)
        db.flush()
        values = dict(symbol_id=symbol.id, timeframe="1h", liquidity_pool_id=liquidity.id, direction="bullish", sweep_type="sell_side_sweep", sweep_candle_id=second.id, liquidity_price=100, extreme_price=99, penetration_percentage=1, rejection_strength=1, status="candidate", confidence_score=60, detected_at=second.close_time, metadata_json={})
        db.add(LiquiditySweep(**values))
        db.flush()
        db.add(LiquiditySweep(**values))
        with pytest.raises(IntegrityError):
            db.flush()
