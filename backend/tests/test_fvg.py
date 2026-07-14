from decimal import Decimal
from types import SimpleNamespace
from conftest import make_candle
from app.fvg.detector import FVGConfig, detect_fvg, mitigation_update


def test_bullish_and_bearish_fvg():
    bullish=[make_candle(0,open_=95,high=100,low=90,close=98),make_candle(1,open_=99,high=120,low=98,close=118),make_candle(2,open_=112,high=125,low=110,close=120)]
    bearish=[make_candle(0,open_=120,high=125,low=115,close=118),make_candle(1,open_=116,high=117,low=90,close=92),make_candle(2,open_=100,high=105,low=85,close=90)]
    assert detect_fvg(bullish,10,config=FVGConfig(min_atr_fraction=.1)).direction == 'bullish'
    assert detect_fvg(bearish,10,config=FVGConfig(min_atr_fraction=.1)).direction == 'bearish'


def test_no_gap_and_tiny_gap_rejection():
    no_gap=[make_candle(0,high=100),make_candle(1,open_=100,high=110,low=95,close=108),make_candle(2,low=99)]
    tiny=[make_candle(0,high=100),make_candle(1,open_=100,high=110,low=99,close=109),make_candle(2,low=101)]
    assert detect_fvg(no_gap,10) is None
    assert detect_fvg(tiny,10,config=FVGConfig(min_atr_fraction=.2)) is None


def test_partial_full_and_invalidation_lifecycle():
    zone=SimpleNamespace(direction='bullish',lower_price=Decimal('100'),upper_price=Decimal('110'),status='active',mitigation_percentage=Decimal('0'))
    assert mitigation_update(zone,make_candle(1,low=105,close=108)) == ('partially_mitigated',Decimal('50.0'))
    assert mitigation_update(zone,make_candle(2,low=99,close=102))[0] == 'fully_mitigated'
    assert mitigation_update(zone,make_candle(3,low=95,close=99))[0] == 'invalidated'

