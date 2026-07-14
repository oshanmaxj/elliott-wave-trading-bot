from typing import Any

import numpy as np
import pandas as pd


def _last_or_none(series: pd.Series) -> float | None:
    if series.empty or pd.isna(series.iloc[-1]):
        return None
    return float(series.iloc[-1])


def calculate_indicators(candles: list[Any]) -> dict[str, float | None]:
    if not candles:
        return {key: None for key in ("ema20", "ema50", "ema200", "rsi14", "macd", "macd_signal", "macd_histogram", "atr14", "average_volume20", "volume_ratio", "price_change_pct")}
    frame = pd.DataFrame({
        "high": [float(c.high) for c in candles], "low": [float(c.low) for c in candles],
        "close": [float(c.close) for c in candles], "volume": [float(c.volume) for c in candles],
    })
    close, high, low, volume = frame.close, frame.high, frame.low, frame.volume
    result: dict[str, float | None] = {}
    for period in (20, 50, 200):
        ema = close.ewm(span=period, adjust=False, min_periods=period).mean()
        result[f"ema{period}"] = _last_or_none(ema)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    if len(close) >= 15 and loss.iloc[-1] == 0:
        rsi.iloc[-1] = 100.0 if gain.iloc[-1] > 0 else 50.0
    result["rsi14"] = _last_or_none(rsi)
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    result["macd"] = _last_or_none(macd)
    result["macd_signal"] = _last_or_none(signal)
    result["macd_histogram"] = _last_or_none(macd - signal)
    previous_close = close.shift(1)
    true_range = pd.concat([(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
    result["atr14"] = _last_or_none(true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    avg_volume = volume.rolling(20, min_periods=20).mean()
    result["average_volume20"] = _last_or_none(avg_volume)
    result["volume_ratio"] = None if pd.isna(avg_volume.iloc[-1]) or avg_volume.iloc[-1] == 0 else float(volume.iloc[-1] / avg_volume.iloc[-1])
    result["price_change_pct"] = None if len(close) < 2 or close.iloc[-2] == 0 else float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
    return result

