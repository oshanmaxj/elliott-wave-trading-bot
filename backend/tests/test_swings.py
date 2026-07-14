from conftest import make_candle
from app.structure.swings import detect_confirmed_pivot


def test_swing_high_and_low_detection():
    highs = [2,3,4,10,5,4,3]; lows = [8,7,6,1,5,6,7]
    candles = [make_candle(i, high=h, low=l, open_=l+1, close=l+2) for i,(h,l) in enumerate(zip(highs,lows))]
    found = detect_confirmed_pivot(candles)
    assert {item.swing_type for item in found} == {"high", "low"}


def test_equal_highs_and_lows_are_not_swings():
    candles = [make_candle(i, high=h, low=l) for i,(h,l) in enumerate(zip([2,3,10,10,5,4,3],[8,7,1,1,5,6,7]))]
    assert detect_confirmed_pivot(candles) == []


def test_insufficient_or_open_right_candle_rejected():
    assert detect_confirmed_pivot([make_candle(i) for i in range(6)]) == []
    rows = [make_candle(i) for i in range(7)]; rows[-1].is_closed = False
    assert detect_confirmed_pivot(rows) == []

