"""Production-Spot research replay for the Heikin Ashi trend-break strategy.

Rules (confirmed with the project owner), long-only:
  1. 5m trend = direction of the most recent *main* (non-inside-bar) 5m HA
     candle as of decision time. Entries only while bullish.
  2. bearish_ref = most recent main bearish 1m HA candle. The entry fires on
     the first subsequent 1m HA candle that is bullish and whose HA close
     breaks (closes) above bearish_ref's HA high — a wick poking above is not
     enough. A newer main bearish 1m candle always replaces bearish_ref.
  3. bullish_ref_5m = most recent main bullish 5m HA candle (the same candle
     that defines the 5m trend while it is bullish). Its HA low is the
     stop-loss used for position sizing at entry, and keeps updating for as
     long as the trade is open whenever a newer main bullish 5m candle forms
     — no "only tighten" clamp, exactly as described.
  4. Exit the whole position (no partial exits) the moment a 5m bearish HA
     candle's HA close breaks below the *current* bullish_ref_5m low, or on a
     hard stop-loss hit (real candle low touching that same tracked level) —
     hard stop wins same-candle ambiguity, mirroring Wave3 HA's `_simulate`.
"""

from bisect import bisect_right
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import select

from app.models import Candle, HeikinAshiTrendBreakSignal
from app.strategies.heikin_ashi import atr_at, derive_heikin_ashi
from app.strategies.heikin_ashi_trend_break import (
    STRATEGY, breaks_below, entry_signal, is_main_candle,
)

D = Decimal


def _main_flags(ha):
    return [is_main_candle(candle, ha[i - 1] if i > 0 else None) for i, candle in enumerate(ha)]


def _naive(value):
    """Strip tzinfo for ordering comparisons only; SQLite round-trips DateTime(timezone=True)
    columns as naive UTC, unlike Postgres, so a caller-supplied aware bound must not be
    compared directly against a value loaded back from the database."""
    return value.replace(tzinfo=None) if value.tzinfo else value


def _latest_at(timeline, moment):
    """Most recent candle in a chronologically-sorted timeline with close_time <= moment."""
    if not timeline:
        return None
    times = [candle.close_time for candle in timeline]
    index = bisect_right(times, moment) - 1
    return timeline[index] if index >= 0 else None


def _break_events(ha5):
    """Every 5m bearish candle whose HA close breaks the then-current bullish_ref_5m low."""
    events = []
    bullish_ref = None
    flags = _main_flags(ha5)
    for candle, main in zip(ha5, flags):
        if main and candle.direction == "bullish":
            bullish_ref = candle
        if bullish_ref is not None and breaks_below(candle, bullish_ref):
            events.append((candle.close_time, candle.real_close, candle.candle_id))
    return events


def _simulate(entry_price, initial_stop, entry_time, after_1m, break_events, main5_bullish):
    risk = entry_price - initial_stop
    if risk <= 0:
        return None
    relevant_breaks = [event for event in break_events if event[0] > entry_time]
    mfe = mae = D("0")
    last = after_1m[-1]
    exit_price, exit_reason, exit_time, exit_ha = D(last.close), "end_of_research", last.close_time, None
    for candle in after_1m:
        favorable = D(candle.high) - entry_price
        adverse = entry_price - D(candle.low)
        mfe, mae = max(mfe, favorable), max(mae, adverse)
        current_ref = _latest_at(main5_bullish, candle.close_time)
        current_stop = current_ref.low if current_ref is not None else initial_stop
        if D(candle.low) <= current_stop:
            exit_price, exit_reason, exit_time, exit_ha = current_stop, "hard_stop", candle.close_time, None
            break
        signal = next((event for event in relevant_breaks if event[0] <= candle.close_time), None)
        if signal:
            exit_time, exit_price, exit_ha = signal
            exit_reason = "5m_bullish_ref_break"
            break
    return {
        "real_exit": D(exit_price), "exit_reason": exit_reason, "exit_time": exit_time,
        "exit_ha_candle_id": exit_ha, "realized_r": (D(exit_price) - entry_price) / risk,
        "mfe_r": mfe / risk, "mae_r": mae / risk,
    }


def evaluate(db, symbol_id: int, start, end, *, persist=False):
    """Chronological single-timeframe-pair replay bounded by each candle's close_time."""
    warmup = start - timedelta(days=7)
    m1 = list(db.scalars(select(Candle).where(
        Candle.symbol_id == symbol_id, Candle.timeframe == "1m", Candle.is_closed.is_(True),
        Candle.close_time >= warmup, Candle.close_time <= end,
    ).order_by(Candle.close_time, Candle.id)))
    m5 = list(db.scalars(select(Candle).where(
        Candle.symbol_id == symbol_id, Candle.timeframe == "5m", Candle.is_closed.is_(True),
        Candle.close_time >= warmup, Candle.close_time <= end,
    ).order_by(Candle.close_time, Candle.id)))
    if not m1 or not m5:
        return []
    ha1, ha5 = derive_heikin_ashi(m1), derive_heikin_ashi(m5)
    main1_flags, main5_flags = _main_flags(ha1), _main_flags(ha5)
    main5_candles = [candle for candle, main in zip(ha5, main5_flags) if main]
    main5_bullish = [candle for candle in main5_candles if candle.direction == "bullish"]
    main1_bearish = [candle for candle, main in zip(ha1, main1_flags) if main and candle.direction == "bearish"]
    break_events = _break_events(ha5)

    output = []
    triggered_ref_ids: set[int] = set()
    for index, candle in enumerate(ha1):
        decision = candle.close_time
        trend_candle = _latest_at(main5_candles, decision)
        ref = _latest_at(main1_bearish, decision)
        if ref is None or ref.candle_id in triggered_ref_ids:
            continue
        if not entry_signal(trend_candle, ref, candle):
            continue
        triggered_ref_ids.add(ref.candle_id)
        if _naive(decision) < _naive(start) or _naive(decision) > _naive(end):
            continue
        bull_ref5 = _latest_at(main5_bullish, decision)
        if bull_ref5 is None:
            continue
        atr = atr_at(m1, index)
        if atr is None:
            continue
        entry_price = D(candle.real_close)
        initial_stop = bull_ref5.low
        if initial_stop >= entry_price:
            continue
        after = [row for row in m1 if row.close_time > decision]
        if not after:
            continue
        result = _simulate(entry_price, initial_stop, decision, after, break_events, main5_bullish)
        if result is None:
            continue
        fingerprint = sha256(f"{symbol_id}:bullish:{ref.candle_id}".encode()).hexdigest()
        row = HeikinAshiTrendBreakSignal(
            symbol_id=symbol_id, strategy=STRATEGY, direction="bullish", status="closed",
            event_fingerprint=fingerprint, decision_time=decision, closed_at=result["exit_time"],
            bearish_ref_candle_id=ref.candle_id, bullish_ref_candle_id=bull_ref5.candle_id,
            entry_ha_candle_id=candle.candle_id, exit_ha_candle_id=result["exit_ha_candle_id"],
            real_entry=entry_price, real_stop=initial_stop, real_exit=result["real_exit"],
            exit_reason=result["exit_reason"], realized_r=result["realized_r"],
            mfe_r=result["mfe_r"], mae_r=result["mae_r"],
            holding_seconds=max(0, int((result["exit_time"] - decision).total_seconds())),
            volatility_regime="high" if atr / entry_price > D("0.005") else "normal",
        )
        output.append(row)
        if persist:
            exists = db.scalar(select(HeikinAshiTrendBreakSignal.id).where(
                HeikinAshiTrendBreakSignal.event_fingerprint == fingerprint))
            if not exists:
                db.add(row)
    if persist:
        db.commit()
    return output
