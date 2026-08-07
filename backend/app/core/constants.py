TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")
TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}
BINANCE_INTERVALS = {timeframe: timeframe for timeframe in TIMEFRAMES}
TIMEFRAME_BIAS_WEIGHTS = {"4h": 12, "1h": 8, "15m": 4, "5m": 2, "1m": 1}
TIMEFRAME_ANALYSIS_PROFILES = {
    "1m": {"minimum_swing_bars": 5, "fvg_atr_multiplier": 1.25, "sweep_confirmation_candles": 3, "sweep_expiry_candles": 30, "setup_expiry_candles": 120},
    "5m": {"minimum_swing_bars": 4, "fvg_atr_multiplier": 1.10, "sweep_confirmation_candles": 2, "sweep_expiry_candles": 16, "setup_expiry_candles": 48},
    "15m": {"minimum_swing_bars": 3, "fvg_atr_multiplier": 1.0},
    "1h": {"minimum_swing_bars": 3, "fvg_atr_multiplier": 1.0},
    "4h": {"minimum_swing_bars": 3, "fvg_atr_multiplier": 1.0},
}
SUPPORTED_SYMBOLS = {"BTCUSDT", "ETHUSDT"}
SUPPORTED_TIMEFRAMES = frozenset(TIMEFRAMES)
