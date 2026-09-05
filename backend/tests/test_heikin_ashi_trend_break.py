from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

from sqlalchemy import func, select

from app.models import Candle, HeikinAshiTrendBreakSignal, Symbol
from app.strategies.heikin_ashi_trend_break import (
    LIVE_AUTO_EXECUTION_ENABLED, breaks_above, entry_signal, is_main_candle,
)
from app.strategies.heikin_ashi_trend_break_research import (
    _break_events, _latest_at, _simulate, evaluate,
)

T0 = datetime(2025, 6, 1, tzinfo=timezone.utc)


def t(minutes):
    return T0 + timedelta(minutes=minutes)


def ha(candle_id, when, *, high, low, close, direction, real_close=None):
    """A fabricated HACandle-like fixture with exact, hand-chosen attribute values."""
    return SimpleNamespace(
        candle_id=candle_id, close_time=when, high=D(str(high)), low=D(str(low)),
        close=D(str(close)), direction=direction,
        real_close=D(str(real_close if real_close is not None else close)),
    )


def real(when, *, high, low, close):
    return SimpleNamespace(high=D(str(high)), low=D(str(low)), close=D(str(close)), close_time=when)


def candles(rows, timeframe, symbol_id):
    step = timedelta(minutes=1) if timeframe == "1m" else timedelta(minutes=5)
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        opened = T0 + step * i
        out.append(Candle(
            symbol_id=symbol_id, timeframe=timeframe, open_time=opened, close_time=opened + step,
            open=D(str(o)), high=D(str(h)), low=D(str(l)), close=D(str(c)),
            volume=D("100"), quote_volume=D("10000"), trade_count=10,
            taker_buy_base_volume=D("50"), taker_buy_quote_volume=D("5000"), is_closed=True,
        ))
    return out


def test_is_main_candle_identifies_inside_bar_vs_extending_candle():
    first = SimpleNamespace(high=D("10"), low=D("5"))
    inside = SimpleNamespace(high=D("9"), low=D("6"))
    extends_high = SimpleNamespace(high=D("11"), low=D("6"))
    extends_low = SimpleNamespace(high=D("9"), low=D("4"))
    assert is_main_candle(first, None) is True
    assert is_main_candle(inside, first) is False
    assert is_main_candle(extends_high, first) is True
    assert is_main_candle(extends_low, first) is True


def test_wick_break_without_a_close_break_is_not_an_entry():
    ref = SimpleNamespace(high=D("100"))
    wick_only = SimpleNamespace(direction="bullish", high=D("150"), close=D("99"))
    close_break = SimpleNamespace(direction="bullish", high=D("150"), close=D("101"))
    assert breaks_above(wick_only, ref) is False
    assert breaks_above(close_break, ref) is True


def test_entry_requires_bullish_5m_trend_even_with_a_valid_1m_close_break():
    ref = SimpleNamespace(high=D("100"))
    candle = SimpleNamespace(direction="bullish", close=D("105"))
    bullish_trend = SimpleNamespace(direction="bullish")
    bearish_trend = SimpleNamespace(direction="bearish")
    assert entry_signal(bullish_trend, ref, candle) is True
    assert entry_signal(bearish_trend, ref, candle) is False
    assert entry_signal(None, ref, candle) is False


def test_bearish_ref_lookup_updates_to_the_newest_main_bearish_candle():
    old_ref = SimpleNamespace(candle_id=1, close_time=t(0), high=D("100"))
    new_ref = SimpleNamespace(candle_id=2, close_time=t(5), high=D("110"))
    timeline = [old_ref, new_ref]
    assert _latest_at(timeline, t(3)) is old_ref
    assert _latest_at(timeline, t(5)) is new_ref
    assert _latest_at(timeline, t(10)) is new_ref
    assert _latest_at(timeline, t(-1)) is None


def test_break_events_fire_only_when_bearish_close_breaks_current_bullish_ref_low():
    bull_ref = ha(1, t(0), high=105, low=95, close=100, direction="bullish")
    non_breaking = ha(2, t(5), high=100, low=96, close=97, direction="bearish")
    breaking = ha(3, t(10), high=97, low=90, close=93, direction="bearish", real_close=92)
    events = _break_events([bull_ref, non_breaking, breaking])
    assert events == [(t(10), D("92"), 3)]


def test_structural_exit_fires_on_5m_bullish_ref_break_without_a_hard_stop():
    ref5 = ha(1, T0, high=105, low=90, close=100, direction="bullish")
    after = [real(t(1), high=102, low=98, close=99)]
    break_events = [(t(1), D("97"), 42)]
    result = _simulate(D("100"), D("90"), T0, after, break_events, [ref5])
    assert result["exit_reason"] == "5m_bullish_ref_break"
    assert result["real_exit"] == D("97")
    assert result["exit_ha_candle_id"] == 42


def test_hard_stop_wins_same_candle_ambiguity_over_structural_exit():
    ref5 = ha(1, T0, high=105, low=95, close=100, direction="bullish")
    candle = real(t(1), high=101, low=94, close=96)
    break_events = [(t(1), D("93"), 99)]  # same close_time as the hard-stop candle
    result = _simulate(D("100"), D("95"), T0, [candle], break_events, [ref5])
    assert result["exit_reason"] == "hard_stop"
    assert result["real_exit"] == D("95")


def test_hard_stop_level_tracks_the_latest_bullish_ref_5m_not_the_entry_time_one():
    ref_at_entry = ha(1, T0, high=105, low=90, close=100, direction="bullish")
    newer_ref = ha(2, t(1), high=115, low=102, close=110, direction="bullish")
    candle = real(t(2), high=112, low=95, close=108)  # 95 is above the old stop (90) but below the new one (102)
    result = _simulate(D("100"), D("90"), T0, [candle], [], [ref_at_entry, newer_ref])
    assert result["exit_reason"] == "hard_stop"
    assert result["real_exit"] == D("102")


def test_strategy_is_research_only():
    assert LIVE_AUTO_EXECUTION_ENABLED is False


def seed_trend_break_fixture(session_factory):
    with session_factory.begin() as db:
        symbol = Symbol(exchange="binance", symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", market_type="spot")
        db.add(symbol)
        db.flush()
        m5_rows = [
            (100, 105, 95, 100),   # neutral baseline (main by default)
            (100, 150, 100, 145),  # clear bullish trend/reference candle
        ]
        m1_rows = [(100, 101, 98, 99)] * 14 + [
            (99, 100, 80, 82),     # sharp dip -> main bearish 1m candle (bearish_ref)
            (82, 160, 82, 155),    # strong bullish close breaks bearish_ref's HA high
            (155, 156, 150, 154),  # trailing candle so `after` is non-empty
        ]
        for row in candles(m5_rows, "5m", symbol.id):
            db.add(row)
        for row in candles(m1_rows, "1m", symbol.id):
            db.add(row)
        db.flush()
        return symbol.id


def test_fingerprint_is_deterministic_and_persist_does_not_duplicate(session_factory):
    symbol_id = seed_trend_break_fixture(session_factory)
    start, end = T0, T0 + timedelta(minutes=30)

    with session_factory() as db:
        first_pass = evaluate(db, symbol_id, start, end, persist=False)
    with session_factory() as db:
        second_pass = evaluate(db, symbol_id, start, end, persist=False)

    assert len(first_pass) == 1 and len(second_pass) == 1
    assert first_pass[0].direction == "bullish"
    assert first_pass[0].event_fingerprint == second_pass[0].event_fingerprint

    with session_factory() as db:
        evaluate(db, symbol_id, start, end, persist=True)
    with session_factory() as db:
        evaluate(db, symbol_id, start, end, persist=True)

    with session_factory() as db:
        count = db.scalar(select(func.count(HeikinAshiTrendBreakSignal.id)))
        persisted = db.scalar(select(HeikinAshiTrendBreakSignal))
    assert count == 1
    assert persisted.event_fingerprint == first_pass[0].event_fingerprint
    assert persisted.exit_reason in {"end_of_research", "hard_stop", "5m_bullish_ref_break"}
