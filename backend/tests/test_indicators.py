from conftest import make_candle
from app.indicators.service import calculate_indicators


def test_indicators_handle_insufficient_history():
    result = calculate_indicators([make_candle(i, close=100 + i) for i in range(10)])
    assert result["ema20"] is None and result["rsi14"] is None and result["atr14"] is None


def test_indicator_calculations_without_lookahead():
    candles = [make_candle(i, high=102+i, low=98+i, close=100+i, volume=100+i) for i in range(220)]
    before = calculate_indicators(candles[:200])
    after = calculate_indicators(candles)
    assert before["ema200"] is not None and after["ema200"] > before["ema200"]
    assert after["rsi14"] == 100.0
    assert after["average_volume20"] is not None and after["volume_ratio"] is not None

