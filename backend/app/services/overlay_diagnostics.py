"""Trace chart overlays to their canonical database and candle provenance."""

from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Candle,
    ElliottWaveCount,
    ElliottWavePoint,
    FVGZone,
    LiquiditySweep,
    MarketStructureEvent,
    OrderBlock,
    SwingPoint,
    Symbol,
    TradeSetup,
)


def _decimal(value):
    return str(value) if value is not None else None


def overlay_diagnostics(db, symbol: str, timeframe: str) -> list[dict]:
    symbol_row = db.scalar(select(Symbol).where(Symbol.symbol == symbol.upper()))
    if not symbol_row:
        return []
    result = []
    candles = {row.id: row for row in db.scalars(select(Candle).where(
        Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe
    ))}
    swings = {row.id: row for row in db.scalars(select(SwingPoint).where(
        SwingPoint.symbol_id == symbol_row.id, SwingPoint.timeframe == timeframe
    ))}

    for row in swings.values():
        candle = candles.get(row.candle_id)
        expected = getattr(candle, row.swing_type, None) if candle else None
        valid = candle is not None and Decimal(row.price) == Decimal(expected)
        result.append({"overlay_type": "swing", "source_table": "swing_points",
            "source_record_id": row.id, "symbol": symbol_row.symbol, "timeframe": timeframe,
            "timestamp": row.detected_at, "candle_ids": [row.candle_id],
            "price_coordinates": {"price": _decimal(row.price)},
            "active": row.invalidated_at is None, "stale": not valid,
            "provenance": {"rule": f"price == candle.{row.swing_type}", "valid": valid}})

    for row in db.scalars(select(MarketStructureEvent).where(
        MarketStructureEvent.symbol_id == symbol_row.id,
        MarketStructureEvent.timeframe == timeframe
    )):
        candle, broken = candles.get(row.confirmation_candle_id), swings.get(row.broken_swing_id)
        valid = candle is not None and broken is not None and Decimal(row.break_price) == Decimal(candle.close)
        result.append({"overlay_type": row.event_type.lower(), "source_table": "market_structure_events",
            "source_record_id": row.id, "symbol": symbol_row.symbol, "timeframe": timeframe,
            "timestamp": row.detected_at, "candle_ids": [row.confirmation_candle_id],
            "price_coordinates": {"break_price": _decimal(row.break_price)},
            "active": True, "stale": not valid,
            "provenance": {"broken_swing_id": row.broken_swing_id,
                           "rule": "break_price == confirmation_candle.close", "valid": valid}})

    for row in db.scalars(select(FVGZone).where(FVGZone.symbol_id == symbol_row.id, FVGZone.timeframe == timeframe)):
        source = [candles.get(value) for value in (row.first_candle_id, row.middle_candle_id, row.third_candle_id)]
        prices = {Decimal(value) for candle in source if candle for value in (candle.high, candle.low)}
        valid = all(source) and Decimal(row.upper_price) in prices and Decimal(row.lower_price) in prices
        result.append({"overlay_type": "fvg", "source_table": "fvg_zones", "source_record_id": row.id,
            "symbol": symbol_row.symbol, "timeframe": timeframe, "timestamp": row.detected_at,
            "candle_ids": [row.first_candle_id, row.middle_candle_id, row.third_candle_id],
            "price_coordinates": {"upper": _decimal(row.upper_price), "lower": _decimal(row.lower_price)},
            "active": row.status in {"active", "partially_mitigated"}, "stale": not valid,
            "provenance": {"rule": "boundaries originate from three source candle highs/lows", "valid": valid}})

    for row in db.scalars(select(OrderBlock).where(OrderBlock.symbol_id == symbol_row.id, OrderBlock.timeframe == timeframe)):
        candle = candles.get(row.candle_id)
        valid = candle is not None and {Decimal(row.top_price), Decimal(row.bottom_price)} == {Decimal(candle.high), Decimal(candle.low)}
        result.append({"overlay_type": "order_block", "source_table": "order_blocks", "source_record_id": row.id,
            "symbol": symbol_row.symbol, "timeframe": timeframe, "timestamp": row.detected_at,
            "candle_ids": [row.candle_id], "price_coordinates": {"top": _decimal(row.top_price), "bottom": _decimal(row.bottom_price)},
            "active": row.status in {"active", "partially_mitigated"}, "stale": not valid,
            "provenance": {"bos_event_id": row.bos_event_id, "rule": "boundaries equal source candle high/low", "valid": valid}})

    for row in db.scalars(select(LiquiditySweep).where(LiquiditySweep.symbol_id == symbol_row.id, LiquiditySweep.timeframe == timeframe)):
        candle = candles.get(row.sweep_candle_id)
        valid = candle is not None and Decimal(row.extreme_price) in {Decimal(candle.high), Decimal(candle.low)}
        result.append({"overlay_type": "liquidity_sweep", "source_table": "liquidity_sweeps", "source_record_id": row.id,
            "symbol": symbol_row.symbol, "timeframe": timeframe, "timestamp": row.detected_at,
            "candle_ids": [row.sweep_candle_id, row.confirmation_candle_id],
            "price_coordinates": {"liquidity": _decimal(row.liquidity_price), "extreme": _decimal(row.extreme_price), "reclaimed": _decimal(row.reclaimed_price)},
            "active": row.status == "confirmed", "stale": not valid,
            "provenance": {"liquidity_pool_id": row.liquidity_pool_id, "rule": "extreme equals sweep candle high/low", "valid": valid}})

    counts = list(db.scalars(select(ElliottWaveCount).where(ElliottWaveCount.symbol_id == symbol_row.id, ElliottWaveCount.timeframe == timeframe)))
    for count in counts:
        for point in db.scalars(select(ElliottWavePoint).where(ElliottWavePoint.wave_count_id == count.id)):
            candle, swing = candles.get(point.candle_id), swings.get(point.swing_point_id)
            valid = (candle is not None and swing is not None and point.candle_id == swing.candle_id
                     and Decimal(point.price) == Decimal(swing.price)
                     and Decimal(point.price) in {Decimal(candle.high), Decimal(candle.low)})
            result.append({"overlay_type": "elliott_point", "source_table": "elliott_wave_points",
                "source_record_id": point.id, "symbol": symbol_row.symbol, "timeframe": timeframe,
                "timestamp": point.timestamp, "range": {"count_id": count.id,
                    "start_candle_id": count.start_candle_id, "end_candle_id": count.end_candle_id},
                "candle_ids": [point.candle_id], "price_coordinates": {"price": _decimal(point.price)},
                "active": count.status in {"primary", "alternate"}, "stale": not valid,
                "provenance": {"wave_count_id": count.id, "swing_point_id": point.swing_point_id,
                               "rule": "point == source swing == source candle high/low", "valid": valid}})

    for row in db.scalars(select(TradeSetup).where(TradeSetup.symbol_id == symbol_row.id, TradeSetup.setup_timeframe == timeframe)):
        coordinates = {key: _decimal(getattr(row, key)) for key in
            ("entry_min", "entry_max", "preferred_entry", "stop_loss", "take_profit_1", "take_profit_2", "take_profit_3")}
        source_valid = any(item["source_record_id"] == row.structure_event_id and not item["stale"]
                           for item in result if item["source_table"] == "market_structure_events")
        result.append({"overlay_type": "trade_setup", "source_table": "trade_setups", "source_record_id": row.id,
            "symbol": symbol_row.symbol, "timeframe": timeframe, "timestamp": row.detected_at,
            "price_coordinates": coordinates, "active": row.status in {"ready", "triggered"},
            "stale": not source_valid, "provenance": {"structure_event_id": row.structure_event_id,
                "fvg_zone_id": row.fvg_zone_id, "order_block_id": row.order_block_id,
                "elliott_wave_count_id": row.elliott_wave_count_id,
                "rule": "geometry retains valid structure/zone/wave source chain", "valid": source_valid}})
    return result
