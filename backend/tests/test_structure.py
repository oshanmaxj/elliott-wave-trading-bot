from decimal import Decimal
from types import SimpleNamespace
from conftest import make_candle
from app.structure.engine import classify_trend, detect_structure_break


def swing(id_, kind, price, candle): return SimpleNamespace(id=id_, swing_type=kind, price=Decimal(str(price)), candle=candle)


def test_bullish_bos_from_protected_high():
    swings=[swing(1,'high',110,make_candle(0)),swing(2,'low',90,make_candle(1)),swing(3,'high',120,make_candle(2)),swing(4,'low',100,make_candle(3))]
    assert classify_trend(swings) == 'bullish'
    signal=detect_structure_break(make_candle(5,high=126,close=125),make_candle(4,high=119,close=118),swings)
    assert signal.event_type == 'BOS' and signal.direction == 'bullish'


def test_bullish_choch_from_bearish_structure():
    swings=[swing(1,'high',130,make_candle(0)),swing(2,'low',110,make_candle(1)),swing(3,'high',120,make_candle(2)),swing(4,'low',100,make_candle(3))]
    signal=detect_structure_break(make_candle(5,high=125,close=121),make_candle(4,high=119,close=118),swings)
    assert classify_trend(swings) == 'bearish' and signal.event_type == 'CHoCH'


def test_wick_only_break_is_configurable():
    swings=[swing(1,'high',110,make_candle(0)),swing(2,'low',90,make_candle(1)),swing(3,'high',120,make_candle(2)),swing(4,'low',100,make_candle(3))]
    candle=make_candle(5,high=125,close=119)
    assert detect_structure_break(candle,make_candle(4,close=118),swings) is None
    assert detect_structure_break(candle,make_candle(4,high=119,close=118),swings,True).event_type == 'BOS'

