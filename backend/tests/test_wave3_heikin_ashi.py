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


def bearish_fixture():
    return candles([(100, 102, 99, 101)] * 14 + [(101, 104, 101, 103), (103, 106, 103, 105), (105, 105, 99, 100), (100, 101, 97, 98)])


def test_confirmed_reversal_diagnostics_reports_confirmed_reversal_with_full_values():
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    diagnostics = {}
    signal = confirmed_reversal(ha, real, len(real) - 1, "bullish", body_atr_min_ratio=D("0.01"),
                                 wick_body_max_ratio=D("10"), diagnostics=diagnostics)
    assert signal is not None
    assert diagnostics["reason"] == "confirmed_reversal"
    assert diagnostics["pullback_min"] == 2
    assert diagnostics["confirmation_required"] is True
    assert diagnostics["wick_body_max_ratio"] == "10"
    assert diagnostics["body_atr_min_ratio"] == "0.01"
    assert diagnostics["reversal_direction"] == "bullish"
    assert diagnostics["confirmation_direction"] == "bullish"
    assert diagnostics["pullback_directions"] == ["bearish", "bearish"]
    assert diagnostics["reversal_real_high"] == "101"
    assert diagnostics["reversal_real_low"] == "95"
    assert diagnostics["confirmation_real_high"] == "103"
    assert diagnostics["confirmation_real_low"] == "99"
    assert D(diagnostics["body"]) == D(signal["body"])
    assert D(diagnostics["atr"]) == D(signal["atr"])
    assert D(diagnostics["body_atr_ratio"]) == D(diagnostics["body"]) / D(diagnostics["atr"])
    assert D(diagnostics["wick_body_ratio"]) == D(signal["wick_body_ratio"])
    assert D(diagnostics["wick"]) / D(diagnostics["body"]) == D(diagnostics["wick_body_ratio"])


def test_confirmed_reversal_diagnostics_reports_insufficient_history():
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    diagnostics = {}
    # confirmation_index=0 with confirmation_required=True gives reversal_index=-1, below pullback_min.
    assert confirmed_reversal(ha, real, 0, "bullish", body_atr_min_ratio=D("0.01"),
                               wick_body_max_ratio=D("10"), diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "insufficient_history"


def test_confirmed_reversal_diagnostics_reports_reversal_direction_failed():
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    diagnostics = {}
    # This exact fixture confirms a BULLISH reversal - asking for "bearish" must mismatch the reversal candle's own direction.
    assert confirmed_reversal(ha, real, len(real) - 1, "bearish", body_atr_min_ratio=D("0.01"),
                               wick_body_max_ratio=D("10"), diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "reversal_direction_failed"
    assert diagnostics["reversal_direction"] == "bullish"


def test_confirmed_reversal_diagnostics_reports_pullback_sequence_failed():
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    diagnostics = {}
    # Proven None case (see test_bullish_reversal_requires_pullback_wick_atr_and_real_breakout):
    # widening the pullback window to 4 candles pulls in non-bearish rows.
    assert confirmed_reversal(ha, real, len(real) - 1, "bullish", pullback_min=4, body_atr_min_ratio=D("0.01"),
                               wick_body_max_ratio=D("10"), diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "pullback_sequence_failed"
    assert diagnostics["pullback_min"] == 4


def test_confirmed_reversal_diagnostics_reports_body_atr_failed():
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    diagnostics = {}
    # Proven None case: an unreachably high body/ATR requirement.
    assert confirmed_reversal(ha, real, len(real) - 1, "bullish", body_atr_min_ratio=D("10"),
                               wick_body_max_ratio=D("10"), diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "body_atr_failed"
    assert diagnostics["body_atr_min_ratio"] == "10"
    assert D(diagnostics["body_atr_ratio"]) < D("10")


def test_confirmed_reversal_diagnostics_reports_wick_body_ratio_failed():
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    diagnostics = {}
    # Proven None case: zero tolerance on the wick/body ratio.
    assert confirmed_reversal(ha, real, len(real) - 1, "bullish", body_atr_min_ratio=D("0.01"),
                               wick_body_max_ratio=D("0"), diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "wick_body_ratio_failed"
    assert diagnostics["wick_body_max_ratio"] == "0"
    assert D(diagnostics["wick_body_ratio"]) > D("0")


def test_confirmed_reversal_diagnostics_reports_confirmation_direction_failed():
    real = bullish_fixture()
    # Flip the confirmation (last) candle to a clearly bearish real shape while
    # leaving the reversal candle before it untouched.
    real[-1].open, real[-1].high, real[-1].low, real[-1].close = D("102"), D("103"), D("90"), D("91")
    diagnostics = {}
    assert confirmed_reversal(derive_heikin_ashi(real), real, len(real) - 1, "bullish", body_atr_min_ratio=D("0.01"),
                               wick_body_max_ratio=D("10"), diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "confirmation_direction_failed"
    assert diagnostics["reversal_direction"] == "bullish"
    assert diagnostics["confirmation_direction"] == "bearish"


def test_confirmed_reversal_diagnostics_reports_real_price_breakout_failed():
    real = bearish_fixture()
    # This exact mutation is the proven None case from
    # test_bearish_reversal_and_confirmation_failure: confirmation's HA
    # direction stays "bearish" (so it is not a confirmation_direction_failed
    # case), but its real low no longer breaks below the reversal candle's.
    real[-1].close = D("102"); real[-1].low = D("100")
    diagnostics = {}
    assert confirmed_reversal(derive_heikin_ashi(real), real, len(real) - 1, "bearish", body_atr_min_ratio=D("0.01"),
                               wick_body_max_ratio=D("10"), diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "real_price_breakout_failed"
    assert diagnostics["confirmation_direction"] == "bearish"
    assert diagnostics["reversal_real_low"] == "99"
    assert diagnostics["confirmation_real_low"] == "100"


def test_confirmed_reversal_diagnostics_does_not_change_the_returned_signal():
    """The diagnostics parameter is purely additive - passing it must never
    change which candle is returned as a signal."""
    real = bullish_fixture(); ha = derive_heikin_ashi(real)
    without = confirmed_reversal(ha, real, len(real) - 1, "bullish", body_atr_min_ratio=D("0.01"), wick_body_max_ratio=D("10"))
    with_diag = confirmed_reversal(ha, real, len(real) - 1, "bullish", body_atr_min_ratio=D("0.01"),
                                    wick_body_max_ratio=D("10"), diagnostics={})
    assert without == with_diag


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

