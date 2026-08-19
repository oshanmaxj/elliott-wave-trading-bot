from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    Candle,
    ElliottWaveCount,
    ElliottWavePoint,
    MarketStructureEvent,
    SwingPoint,
)
from app.repositories.market import ensure_symbol
from app.services.derived_regeneration import DerivedRegenerationService
from app.services.overlay_diagnostics import overlay_diagnostics


def candle(symbol_id, timeframe, opened, low, high, close):
    return Candle(
        symbol_id=symbol_id, timeframe=timeframe, open_time=opened,
        close_time=opened + timedelta(hours=1) - timedelta(milliseconds=1),
        open=Decimal("100"), high=Decimal(high), low=Decimal(low),
        close=Decimal(close), volume=1, quote_volume=1, trade_count=1,
        taker_buy_base_volume=1, taker_buy_quote_volume=1, is_closed=True,
    )


@pytest.mark.asyncio
async def test_regeneration_removes_stale_elliott_and_rebuilds_structure_without_cross_scope_deletion(
    session_factory,
):
    start = datetime(2026, 8, 17, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        btc, eth = ensure_symbol(db, "BTCUSDT"), ensure_symbol(db, "ETHUSDT")
        rows = [candle(btc.id, "1h", start + timedelta(hours=i), "90", "110", "101") for i in range(3)]
        other_symbol = candle(eth.id, "1h", start, "190", "210", "201")
        other_timeframe = candle(btc.id, "5m", start, "95", "105", "101")
        db.add_all([*rows, other_symbol, other_timeframe])
        db.flush()
        stale = SwingPoint(symbol_id=btc.id, timeframe="1h", candle_id=rows[0].id,
            swing_type="low", price=Decimal("1"), strength=Decimal("1"),
            confirmation_candles=1, detected_at=rows[1].close_time, metadata_json={})
        keep_symbol = SwingPoint(symbol_id=eth.id, timeframe="1h", candle_id=other_symbol.id,
            swing_type="low", price=other_symbol.low, strength=Decimal("1"),
            confirmation_candles=1, detected_at=other_symbol.close_time, metadata_json={})
        keep_timeframe = SwingPoint(symbol_id=btc.id, timeframe="5m", candle_id=other_timeframe.id,
            swing_type="low", price=other_timeframe.low, strength=Decimal("1"),
            confirmation_candles=1, detected_at=other_timeframe.close_time, metadata_json={})
        db.add_all([stale, keep_symbol, keep_timeframe])
        db.flush()
        broken = MarketStructureEvent(symbol_id=btc.id, timeframe="1h", event_type="BOS",
            direction="bullish", broken_swing_id=stale.id, confirmation_candle_id=rows[2].id,
            break_price=Decimal("1"), previous_trend="range", resulting_trend="bullish",
            confidence=Decimal("80"), metadata_json={}, detected_at=rows[2].close_time)
        db.add(broken)
        db.flush()
        count = ElliottWaveCount(symbol_id=btc.id, timeframe="1h", degree="minor",
            direction="bullish", pattern_type="impulse", status="primary", rank=1,
            confidence_score=Decimal("80"), start_candle_id=rows[0].id,
            end_candle_id=rows[2].id, invalidation_price=Decimal("1"),
            rules_passed_json=[], rules_failed_json=[], fibonacci_scores_json={},
            structure_confirmation_json={}, liquidity_confirmation_json={}, metadata_json={},
            detected_at=rows[2].close_time)
        db.add(count)
        db.flush()
        db.add(ElliottWavePoint(wave_count_id=count.id, wave_label="0", sequence_number=0,
            swing_point_id=stale.id, candle_id=rows[0].id, price=Decimal("1"),
            timestamp=rows[0].open_time, duration_bars=0, metadata_json={}))
        target_ids = [row.id for row in rows]

    async def rebuild(candle_id, **kwargs):
        if candle_id != target_ids[-1]:
            return {"processed": True}
        with session_factory.begin() as db:
            source = db.get(Candle, target_ids[0])
            confirmation = db.get(Candle, target_ids[-1])
            swing = SwingPoint(symbol_id=source.symbol_id, timeframe="1h", candle_id=source.id,
                swing_type="low", price=source.low, strength=Decimal("1"),
                confirmation_candles=1, detected_at=confirmation.close_time, metadata_json={})
            db.add(swing)
            db.flush()
            db.add(MarketStructureEvent(symbol_id=source.symbol_id, timeframe="1h",
                event_type="BOS", direction="bullish", broken_swing_id=swing.id,
                confirmation_candle_id=confirmation.id, break_price=confirmation.close,
                previous_trend="range", resulting_trend="bullish", confidence=Decimal("80"),
                metadata_json={}, detected_at=confirmation.close_time))
        return {"processed": True}

    service = DerivedRegenerationService(session_factory, rebuild)
    dry = await service.run("BTCUSDT", "1h", start, start + timedelta(hours=4))
    assert not dry["apply"] and dry["counts"]["elliott_wave_counts"] == 1
    first = await service.run("BTCUSDT", "1h", start, start + timedelta(hours=4), apply=True)
    second = await service.run("BTCUSDT", "1h", start, start + timedelta(hours=4), apply=True)
    assert first["failed"] == second["failed"] == 0
    with session_factory() as db:
        btc = ensure_symbol(db, "BTCUSDT")
        assert not list(db.scalars(select(ElliottWavePoint)))
        swings = list(db.scalars(select(SwingPoint).where(SwingPoint.symbol_id == btc.id, SwingPoint.timeframe == "1h")))
        structures = list(db.scalars(select(MarketStructureEvent).where(MarketStructureEvent.symbol_id == btc.id, MarketStructureEvent.timeframe == "1h")))
        assert len(swings) == len(structures) == 1
        assert swings[0].price == Decimal("90") and structures[0].break_price == Decimal("101")
        assert len(list(db.scalars(select(SwingPoint).where(SwingPoint.timeframe == "5m")))) == 1
        assert len(overlay_diagnostics(db, "ETHUSDT", "1h")) == 1
        assert not [row for row in overlay_diagnostics(db, "BTCUSDT", "1h") if row["stale"]]
