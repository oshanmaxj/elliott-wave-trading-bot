from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

from app.execution.strategies import runtime_strategy_for_setup_name
from app.strategies.heikin_ashi import derive_heikin_ashi, confirmed_reversal
from app.strategies.elliott_wave3_heikin_ashi_reversal import (
    LIVE_AUTO_EXECUTION_ENABLED, exit_priority, score_components, total_score,
    variant_b_fractions, wave3_gate,
)


def candles(rows, closed=True):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [SimpleNamespace(id=i + 1, open_time=start + timedelta(minutes=i),
        close_time=start + timedelta(minutes=i + 1), open=D(str(o)), high=D(str(h)),
        low=D(str(l)), close=D(str(c)), volume=D("100"), is_closed=closed)
        for i, (o, h, l, c) in enumerate(rows)]


def test_ha_formula_and_deterministic_initialization_without_ohlc_mutation():
    real = candles([(10, 14, 8, 12), (12, 15, 10, 14)])
    before = [(x.open, x.high, x.low, x.close) for x in real]
    ha = derive_heikin_ashi(real)
    assert (ha[0].open, ha[0].close, ha[0].high, ha[0].low) == (D("11"), D("11"), D("14"), D("8"))
    assert (ha[1].open, ha[1].close, ha[1].high, ha[1].low) == (D("11"), D("12.75"), D("15"), D("10"))
    assert before == [(x.open, x.high, x.low, x.close) for x in real]


def bullish_fixture():
    base = [(100, 101, 98, 99)] * 14
    return candles(base + [(99, 100, 96, 97), (97, 98, 94, 95), (95, 101, 95, 100), (100, 103, 99, 102)])


def test_bullish_reversal_requires_pullback_wick_atr_and_real_breakout():
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    signal = confirmed_reversal(ha, real, len(real) - 1, "bullish", body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("10"))
    assert signal and signal["real_entry"] == "102" and signal["confirmation_candle_id"] == len(real)
    assert confirmed_reversal(ha, real, len(real)-1, "bullish", pullback_min=4, body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("10")) is None
    assert confirmed_reversal(ha, real, len(real)-1, "bullish", body_atr_min_ratio=D("10"), wick_body_max_ratio=D("10")) is None
    assert confirmed_reversal(ha, real, len(real)-1, "bullish", body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("0")) is None


def test_bearish_reversal_and_confirmation_failure():
    real = candles([(100, 102, 99, 101)] * 14 + [(101, 104, 101, 103), (103, 106, 103, 105), (105, 105, 99, 100), (100, 101, 97, 98)])
    ha = derive_heikin_ashi(real)
    assert confirmed_reversal(ha, real, len(real)-1, "bearish", body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("10"))
    real[-1].close = D("102"); real[-1].low = D("100")
    assert confirmed_reversal(derive_heikin_ashi(real), real, len(real)-1, "bearish", body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("10")) is None


def test_future_candles_cannot_change_historical_signal():
    real = bullish_fixture(); index = len(real)-1
    first = confirmed_reversal(derive_heikin_ashi(real), real, index, "bullish", body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("10"))
    future = candles([(1, 999, 1, 500)])[0]; future.id = 999; future.open_time = real[-1].close_time + timedelta(minutes=1); future.close_time = future.open_time + timedelta(minutes=1); real.append(future)
    second = confirmed_reversal(derive_heikin_ashi(real), real, index, "bullish", body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("10"))
    assert first == second


def test_wave3_gate_invalidation_scoring_variants_and_exit_priority():
    now = datetime.now(timezone.utc)
    points = [SimpleNamespace(id=i, sequence_number=i) for i in range(3)]
    count = SimpleNamespace(timeframe="15m", detected_at=now, invalidated_at=None,
        points=points, metadata_json={"current_wave": "3"}, pattern_type="bullish_impulse",
        direction="bullish", invalidation_price=D("90"))
    assert wave3_gate(count, now, D("100")) == (True, [0, 1, 2])
    assert wave3_gate(count, now, D("89"))[0] is False
    components = score_components(bos=True, choch_bos=True, ha_reversal=True, alignment=True)
    assert total_score(components) == 65
    assert variant_b_fractions(True, True) == (D("0.50"), D("0.50"))
    assert exit_priority(hard_stop=True, invalidated=True, opposite_ha=True) == "hard_stop"


def test_strategy_is_research_only_and_not_execution_routable():
    assert LIVE_AUTO_EXECUTION_ENABLED is False
    assert runtime_strategy_for_setup_name("elliott_wave3_heikin_ashi_reversal") is None

