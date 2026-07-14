from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from app.database.session import SessionLocal
from app.fvg.detector import FVGConfig, detect_fvg, mitigation_update
from app.indicators.service import calculate_indicators
from app.models import Alert, AnalysisSnapshot, Candle, FVGZone, LiquidityPool, MarketStructureEvent, OrderBlock, SwingPoint
from app.smc.engine import detect_liquidity, detect_order_block, order_block_mitigation
from app.services.broadcast import broadcaster
from app.services.settings import get_runtime_settings
from app.structure.engine import classify_trend, detect_structure_break
from app.structure.swings import detect_confirmed_pivot


def serialize(row) -> dict:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, (datetime, Decimal)):
            value = value.isoformat() if isinstance(value, datetime) else str(value)
        result[column.name] = value
    return result


def add_alert(db, events, alert_type, symbol_id, timeframe, message, source_type, source_id, created_at):
    alert = db.scalar(select(Alert).where(Alert.type == alert_type, Alert.source_type == source_type, Alert.source_id == source_id))
    if alert:
        return alert
    alert = Alert(type=alert_type, symbol_id=symbol_id, timeframe=timeframe, message=message, source_type=source_type, source_id=source_id, created_at=created_at)
    db.add(alert)
    db.flush()
    events.append(("alert", serialize(alert)))
    return alert


async def process_closed_candle(candle_id: int, broadcast: bool = True, session_factory=None) -> dict:
    events: list[tuple[str, dict]] = []
    session_factory = session_factory or SessionLocal
    with session_factory() as db:
        candle = db.get(Candle, candle_id)
        if not candle or not candle.is_closed:
            return {"processed": False, "reason": "candle_not_closed"}
        settings = get_runtime_settings(db)
        candles = list(db.scalars(select(Candle).where(Candle.symbol_id == candle.symbol_id, Candle.timeframe == candle.timeframe, Candle.is_closed.is_(True), Candle.open_time <= candle.open_time).order_by(Candle.open_time.desc()).limit(250)))
        candles.reverse()
        indicators = calculate_indicators(candles)
        indicators["_smc_version"] = 2
        for candidate in detect_confirmed_pivot(candles, settings.swing_left_bars, settings.swing_right_bars):
            existing = db.scalar(select(SwingPoint).where(SwingPoint.candle_id == candidate.candle.id, SwingPoint.swing_type == candidate.swing_type))
            if not existing:
                swing = SwingPoint(symbol_id=candle.symbol_id, timeframe=candle.timeframe, candle_id=candidate.candle.id, swing_type=candidate.swing_type, price=candidate.price, strength=candidate.strength, confirmation_candles=settings.swing_right_bars, detected_at=candle.close_time, metadata_json={"left_bars": settings.swing_left_bars, "right_bars": settings.swing_right_bars})
                db.add(swing)
                db.flush()
                events.append(("swing_point", serialize(swing)))
        swings = list(db.scalars(select(SwingPoint).join(Candle, SwingPoint.candle_id == Candle.id).where(
            SwingPoint.symbol_id == candle.symbol_id,
            SwingPoint.timeframe == candle.timeframe,
            SwingPoint.detected_at <= candle.close_time,
            Candle.open_time < candle.open_time,
        ).order_by(SwingPoint.detected_at, SwingPoint.id)))
        previous = candles[-2] if len(candles) > 1 else None
        signal = detect_structure_break(candle, previous, swings, settings.wick_break_allowed)
        structure_event = None
        if signal and float(signal.confidence) >= settings.structure_confidence_threshold:
            structure_event = db.scalar(select(MarketStructureEvent).where(MarketStructureEvent.event_type == signal.event_type, MarketStructureEvent.broken_swing_id == signal.broken_swing.id, MarketStructureEvent.confirmation_candle_id == candle.id))
            if not structure_event:
                structure_event = MarketStructureEvent(symbol_id=candle.symbol_id, timeframe=candle.timeframe, event_type=signal.event_type, direction=signal.direction, broken_swing_id=signal.broken_swing.id, confirmation_candle_id=candle.id, break_price=candle.close, previous_trend=signal.previous_trend, resulting_trend=signal.resulting_trend, confidence=signal.confidence, metadata_json={"wick_break_allowed": settings.wick_break_allowed}, detected_at=candle.close_time)
                db.add(structure_event)
                db.flush()
                events.append((signal.event_type.lower(), serialize(structure_event)))
            add_alert(db, events, signal.event_type, candle.symbol_id, candle.timeframe, f"New {signal.direction} {signal.event_type}", "structure", structure_event.id, candle.close_time)
        fvg_signal = detect_fvg(candles, indicators.get("atr14"), indicators.get("volume_ratio"), bool(structure_event), FVGConfig(min_atr_fraction=settings.minimum_fvg_atr_size, require_volume_confirmation=settings.fvg_volume_confirmation))
        if fvg_signal and len(candles) >= 3:
            first, middle, third = candles[-3:]
            zone = db.scalar(select(FVGZone).where(FVGZone.first_candle_id == first.id, FVGZone.middle_candle_id == middle.id, FVGZone.third_candle_id == third.id, FVGZone.direction == fvg_signal.direction))
            if not zone:
                zone = FVGZone(symbol_id=candle.symbol_id, timeframe=candle.timeframe, direction=fvg_signal.direction, first_candle_id=first.id, middle_candle_id=middle.id, third_candle_id=third.id, upper_price=fvg_signal.upper_price, lower_price=fvg_signal.lower_price, size_percentage=fvg_signal.size_percentage, status="active", mitigation_percentage=0, detected_at=candle.close_time, metadata_json={"atr_at_detection": indicators.get("atr14")})
                db.add(zone)
                db.flush()
                events.append(("fvg_new", serialize(zone)))
            add_alert(db, events, "NEW_FVG", candle.symbol_id, candle.timeframe, f"New {zone.direction} fair value gap", "fvg", zone.id, candle.close_time)
        active_zones = list(db.scalars(select(FVGZone).where(
            FVGZone.symbol_id == candle.symbol_id,
            FVGZone.timeframe == candle.timeframe,
            FVGZone.detected_at <= candle.close_time,
            FVGZone.status.in_(["active", "partially_mitigated"]),
            FVGZone.third_candle_id != candle.id,
        )))
        for zone in active_zones:
            status, percentage = mitigation_update(zone, candle)
            if status != zone.status or percentage != zone.mitigation_percentage:
                if not zone.first_touched_at and percentage > 0:
                    zone.first_touched_at = candle.close_time
                zone.status, zone.mitigation_percentage = status, percentage
                if status == "fully_mitigated":
                    zone.fully_mitigated_at = candle.close_time
                if status == "invalidated":
                    zone.invalidated_at = candle.close_time
                events.append(("fvg_mitigation", serialize(zone)))
        liquidity_signal = detect_liquidity(swings, settings.liquidity_tolerance_percentage)
        if liquidity_signal:
            pool = db.scalar(select(LiquidityPool).where(LiquidityPool.type == liquidity_signal.type, LiquidityPool.first_swing_id == liquidity_signal.first_swing.id, LiquidityPool.second_swing_id == liquidity_signal.second_swing.id))
            if not pool:
                pool = LiquidityPool(symbol_id=candle.symbol_id, timeframe=candle.timeframe, type=liquidity_signal.type, price=liquidity_signal.price, strength=liquidity_signal.strength, first_swing_id=liquidity_signal.first_swing.id, second_swing_id=liquidity_signal.second_swing.id, detected_at=candle.close_time, metadata_json={"pattern": liquidity_signal.pattern, "tolerance_percentage": settings.liquidity_tolerance_percentage})
                db.add(pool)
                db.flush()
                events.append(("liquidity_new", serialize(pool)))
        active_pools = list(db.scalars(select(LiquidityPool).where(LiquidityPool.symbol_id == candle.symbol_id, LiquidityPool.timeframe == candle.timeframe, LiquidityPool.detected_at <= candle.close_time, LiquidityPool.swept_at.is_(None))))
        for pool in active_pools:
            swept = (pool.type == "BSL" and candle.high > pool.price and candle.close < pool.price) or (pool.type == "SSL" and candle.low < pool.price and candle.close > pool.price)
            if swept:
                pool.swept_at = candle.close_time
                events.append(("liquidity_sweep", serialize(pool)))
                add_alert(db, events, "LIQUIDITY_SWEEP", candle.symbol_id, candle.timeframe, f"{pool.type} liquidity swept at {pool.price}", "liquidity", pool.id, candle.close_time)
        block_signal = detect_order_block(candles, structure_event)
        if block_signal and structure_event:
            block = db.scalar(select(OrderBlock).where(OrderBlock.bos_event_id == structure_event.id))
            if not block:
                block = OrderBlock(symbol_id=candle.symbol_id, timeframe=candle.timeframe, direction=block_signal.direction, candle_id=block_signal.candle.id, top_price=block_signal.top_price, bottom_price=block_signal.bottom_price, bos_event_id=structure_event.id, status="active", mitigation_percent=0, detected_at=candle.close_time)
                db.add(block)
                db.flush()
                events.append(("order_block_new", serialize(block)))
        active_blocks = list(db.scalars(select(OrderBlock).where(OrderBlock.symbol_id == candle.symbol_id, OrderBlock.timeframe == candle.timeframe, OrderBlock.detected_at <= candle.close_time, OrderBlock.status.in_(["active", "partially_mitigated"]), OrderBlock.candle_id != candle.id)))
        for block in active_blocks:
            status, percentage = order_block_mitigation(block, candle)
            if status != block.status or percentage != block.mitigation_percent:
                if not block.first_touched_at and percentage > 0:
                    block.first_touched_at = candle.close_time
                block.status, block.mitigation_percent = status, percentage
                if status == "fully_mitigated":
                    block.fully_mitigated_at = candle.close_time
                if status == "invalidated":
                    block.invalidated_at = candle.close_time
                events.append(("order_block_mitigation", serialize(block)))
                add_alert(db, events, "ORDER_BLOCK_MITIGATION", candle.symbol_id, candle.timeframe, f"{block.direction} order block {status}", "order_block", block.id, candle.close_time)
        trend = classify_trend(swings)
        latest_event = db.scalar(select(MarketStructureEvent).where(
            MarketStructureEvent.symbol_id == candle.symbol_id,
            MarketStructureEvent.timeframe == candle.timeframe,
            MarketStructureEvent.detected_at <= candle.close_time,
        ).order_by(MarketStructureEvent.detected_at.desc(), MarketStructureEvent.id.desc()).limit(1))
        active_count = db.scalar(select(func.count(FVGZone.id)).where(
            FVGZone.symbol_id == candle.symbol_id,
            FVGZone.timeframe == candle.timeframe,
            FVGZone.detected_at <= candle.close_time,
            FVGZone.status.in_(["active", "partially_mitigated"]),
        )) or 0
        snapshot = db.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.symbol_id == candle.symbol_id, AnalysisSnapshot.timeframe == candle.timeframe, AnalysisSnapshot.generated_at == candle.close_time))
        if not snapshot:
            snapshot = AnalysisSnapshot(symbol_id=candle.symbol_id, timeframe=candle.timeframe, trend=trend, latest_structure_event=latest_event.event_type if latest_event else None, active_fvg_count=active_count, indicator_values_json=indicators, confidence_score=latest_event.confidence if latest_event else Decimal("0.5"), generated_at=candle.close_time)
            db.add(snapshot)
            db.flush()
            events.append(("analysis_snapshot", serialize(snapshot)))
        elif snapshot.indicator_values_json.get("_smc_version") != 2:
            snapshot.indicator_values_json = indicators
        db.commit()
        response = {"processed": True, "candle_id": candle_id, "events": len(events)}
    if broadcast:
        for event_type, payload in events:
            await broadcaster.broadcast(event_type, payload)
    return response
