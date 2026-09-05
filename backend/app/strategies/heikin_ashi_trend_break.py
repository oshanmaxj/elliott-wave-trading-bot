"""Research-only Heikin Ashi trend-break strategy: 5m HA trend + 1m HA break entry.

Long-only for now: the strategy was described to us only for the bullish case
(5m HA uptrend, 1m HA close breaking the most recent main bearish 1m candle's
high). No bearish/short mirror is implemented — that would be an assumption,
not something confirmed with the project owner.
"""

STRATEGY = "heikin_ashi_trend_break"
LIVE_AUTO_EXECUTION_ENABLED = False


def is_main_candle(candle, previous) -> bool:
    """A "main" HA candle is not an inside bar relative to the immediately prior candle.

    An inside candle is fully contained within the previous candle's HA
    high/low range and is skipped as a reference candle everywhere in the
    research replay (bearish_ref, bullish_ref_5m and the 5m trend candle are
    all drawn only from main candles).
    """
    return previous is None or candle.high > previous.high or candle.low < previous.low


def breaks_above(candle, ref) -> bool:
    """A close-confirmed break above ref's HA high; a wick alone does not count."""
    return candle.direction == "bullish" and candle.close > ref.high


def breaks_below(candle, ref) -> bool:
    """A close-confirmed break below ref's HA low; a wick alone does not count."""
    return candle.direction == "bearish" and candle.close < ref.low


def entry_signal(trend_candle, bearish_ref, candle) -> bool:
    """Long entry: the 5m trend must be bullish and this 1m candle must close-break bearish_ref."""
    return (
        trend_candle is not None
        and trend_candle.direction == "bullish"
        and bearish_ref is not None
        and breaks_above(candle, bearish_ref)
    )
