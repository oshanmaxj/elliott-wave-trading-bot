from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError

from app.database.session import SessionLocal
from app.fvg.detector import FVGConfig, detect_fvg, mitigation_update
from app.indicators.service import calculate_indicators
from app.models import AnalysisSnapshot, Candle, FVGZone, MarketStructureEvent, SwingPoint
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


async def process_closed_candle(candle_id: int) -> dict:
    events: list[tuple[str, dict]] = []
    with SessionLocal() as db:
        candle = db.get(Candle, candle_id)
        if not candle or not candle.is_closed:
            return {"processed": False, "reason": "candle_not_closed"}
        settings = get_runtime_settings(db)
        candles = list(db.scalars(select(Candle).where(Candle.symbol_id == candle.symbol_id, Candle.timeframe == candle.timeframe, Candle.is_closed.is_(True), Candle.open_time <= candle.open_time).order_by(Candle.open_time.desc()).limit(250)))
        candles.reverse()
        indicators = calculate_indicators(candles)
        for candidate in detect_confirmed_pivot(candles, settings.swing_left_bars, settings.swing_right_bars):
            existing = db.scalar(select(SwingPoint).where(SwingPoint.candle_id == candidate.candle.id, SwingPoint.swing_type == candidate.swing_type))
            if not existing:
                swing = SwingPoint(symbol_id=candle.symbol_id, timeframe=candle.timeframe, candle_id=candidate.candle.id, swing_type=candidate.swing_type, price=candidate.price, strength=candidate.strength, confirmation_candles=settings.swing_right_bars, detected_at=candle.close_time, metadata_json={"left_bars": settings.swing_left_bars, "right_bars": settings.swing_right_bars})
                db.add(swing); db.flush()
                events.append(("swing_point", serialize(swing)))
        swings = list(db.scalars(select(SwingPoint).where(SwingPoint.symbol_id == candle.symbol_id, SwingPoint.timeframe == candle.timeframe).order_by(SwingPoint.detected_at)))
        previous = candles[-2] if len(candles) > 1 else None
        signal = detect_structure_break(candle, previous, swings, settings.wick_break_allowed)
        structure_event = None
        if signal and float(signal.confidence) >= settings.structure_confidence_threshold:
            structure_event = db.scalar(select(MarketStructureEvent).where(MarketStructureEvent.event_type == signal.event_type, MarketStructureEvent.broken_swing_id == signal.broken_swing.id, MarketStructureEvent.confirmation_candle_id == candle.id))
            if not structure_event:
                structure_event = MarketStructureEvent(symbol_id=candle.symbol_id, timeframe=candle.timeframe, event_type=signal.event_type, direction=signal.direction, broken_swing_id=signal.broken_swing.id, confirmation_candle_id=candle.id, break_price=candle.close, previous_trend=signal.previous_trend, resulting_trend=signal.resulting_trend, confidence=signal.confidence, metadata_json={"wick_break_allowed": settings.wick_break_allowed}, detected_at=candle.close_time)
                db.add(structure_event); db.flush()
                events.append((signal.event_type.lower(), serialize(structure_event)))
        fvg_signal = detect_fvg(candles, indicators.get("atr14"), indicators.get("volume_ratio"), bool(structure_event), FVGConfig(min_atr_fraction=settings.minimum_fvg_atr_size, require_volume_confirmation=settings.fvg_volume_confirmation))
        if fvg_signal and len(candles) >= 3:
            first, middle, third = candles[-3:]
            zone = db.scalar(select(FVGZone).where(FVGZone.first_candle_id == first.id, FVGZone.middle_candle_id == middle.id, FVGZone.third_candle_id == third.id, FVGZone.direction == fvg_signal.direction))
            if not zone:
                zone = FVGZone(symbol_id=candle.symbol_id, timeframe=candle.timeframe, direction=fvg_signal.direction, first_candle_id=first.id, middle_candle_id=middle.id, third_candle_id=third.id, upper_price=fvg_signal.upper_price, lower_price=fvg_signal.lower_price, size_percentage=fvg_signal.size_percentage, status="active", mitigation_percentage=0, detected_at=candle.close_time, metadata_json={"atr_at_detection": indicators.get("atr14")})
                db.add(zone); db.flush()
                events.append(("fvg_new", serialize(zone)))
        active_zones = list(db.scalars(select(FVGZone).where(FVGZone.symbol_id == candle.symbol_id, FVGZone.timeframe == candle.timeframe, FVGZone.status.in_(["active", "partially_mitigated"]), FVGZone.third_candle_id != candle.id)))
        for zone in active_zones:
            status, percentage = mitigation_update(zone, candle)
            if status != zone.status or percentage != zone.mitigation_percentage:
                if not zone.first_touched_at and percentage > 0:
                    zone.first_touched_at = candle.close_time
                zone.status, zone.mitigation_percentage = status, percentage
                if status == "fully_mitigated": zone.fully_mitigated_at = candle.close_time
                if status == "invalidated": zone.invalidated_at = candle.close_time
                events.append(("fvg_mitigation", serialize(zone)))
        trend = classify_trend(swings)
        latest_event = db.scalar(select(MarketStructureEvent).where(MarketStructureEvent.symbol_id == candle.symbol_id, MarketStructureEvent.timeframe == candle.timeframe).order_by(MarketStructureEvent.detected_at.desc()).limit(1))
        active_count = db.scalar(select(func.count(FVGZone.id)).where(FVGZone.symbol_id == candle.symbol_id, FVGZone.timeframe == candle.timeframe, FVGZone.status.in_(["active", "partially_mitigated"]))) or 0
        snapshot = db.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.symbol_id == candle.symbol_id, AnalysisSnapshot.timeframe == candle.timeframe, AnalysisSnapshot.generated_at == candle.close_time))
        if not snapshot:
            snapshot = AnalysisSnapshot(symbol_id=candle.symbol_id, timeframe=candle.timeframe, trend=trend, latest_structure_event=latest_event.event_type if latest_event else None, active_fvg_count=active_count, indicator_values_json=indicators, confidence_score=latest_event.confidence if latest_event else Decimal("0.5"), generated_at=candle.close_time)
            db.add(snapshot); db.flush()
            events.append(("analysis_snapshot", serialize(snapshot)))
        db.commit()
        response = {"processed": True, "candle_id": candle_id, "events": len(events)}
    for event_type, payload in events:
        await broadcaster.broadcast(event_type, payload)
    return response
