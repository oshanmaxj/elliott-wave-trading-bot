from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from app.database.session import SessionLocal
from app.fvg.detector import FVGConfig, detect_fvg, mitigation_update
from app.indicators.service import calculate_indicators
from app.elliott.service import process_elliott_candidates
from app.elliott.setups import select_wave_strategy
from app.models import (
    Alert,
    AnalysisSnapshot,
    Candle,
    FVGZone,
    LiquidityPool,
    LiquiditySweep,
    MarketStructureEvent,
    OrderBlock,
    SwingPoint,
    TradeSetup,
)
from app.smc.engine import (
    detect_liquidity,
    detect_order_block,
    order_block_mitigation,
    premium_discount,
)
from app.smc.setups import generate_setup, update_setup_lifecycle
from app.smc.sweeps import detect_sweep, update_sweep
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


def add_alert(
    db,
    events,
    alert_type,
    symbol_id,
    timeframe,
    message,
    source_type,
    source_id,
    created_at,
):
    alert = db.scalar(
        select(Alert).where(
            Alert.type == alert_type,
            Alert.source_type == source_type,
            Alert.source_id == source_id,
        )
    )
    if alert:
        return alert
    alert = Alert(
        type=alert_type,
        symbol_id=symbol_id,
        timeframe=timeframe,
        message=message,
        source_type=source_type,
        source_id=source_id,
        created_at=created_at,
    )
    db.add(alert)
    db.flush()
    events.append(("alert", serialize(alert)))
    return alert


async def process_closed_candle(
    candle_id: int, broadcast: bool = True, session_factory=None
) -> dict:
    events: list[tuple[str, dict]] = []
    session_factory = session_factory or SessionLocal
    with session_factory() as db:
        candle = db.get(Candle, candle_id)
        if not candle or not candle.is_closed:
            return {"processed": False, "reason": "candle_not_closed"}
        settings = get_runtime_settings(db)
        candles = list(
            db.scalars(
                select(Candle)
                .where(
                    Candle.symbol_id == candle.symbol_id,
                    Candle.timeframe == candle.timeframe,
                    Candle.is_closed.is_(True),
                    Candle.open_time <= candle.open_time,
                )
                .order_by(Candle.open_time.desc())
                .limit(250)
            )
        )
        candles.reverse()
        indicators = calculate_indicators(candles)
        indicators["_smc_version"] = 4
        for candidate in detect_confirmed_pivot(
            candles, settings.swing_left_bars, settings.swing_right_bars
        ):
            existing = db.scalar(
                select(SwingPoint).where(
                    SwingPoint.candle_id == candidate.candle.id,
                    SwingPoint.swing_type == candidate.swing_type,
                )
            )
            if not existing:
                swing = SwingPoint(
                    symbol_id=candle.symbol_id,
                    timeframe=candle.timeframe,
                    candle_id=candidate.candle.id,
                    swing_type=candidate.swing_type,
                    price=candidate.price,
                    strength=candidate.strength,
                    confirmation_candles=settings.swing_right_bars,
                    detected_at=candle.close_time,
                    metadata_json={
                        "left_bars": settings.swing_left_bars,
                        "right_bars": settings.swing_right_bars,
                    },
                )
                db.add(swing)
                db.flush()
                events.append(("swing_point", serialize(swing)))
        swings = list(
            db.scalars(
                select(SwingPoint)
                .join(Candle, SwingPoint.candle_id == Candle.id)
                .where(
                    SwingPoint.symbol_id == candle.symbol_id,
                    SwingPoint.timeframe == candle.timeframe,
                    SwingPoint.detected_at <= candle.close_time,
                    Candle.open_time < candle.open_time,
                )
                .order_by(SwingPoint.detected_at, SwingPoint.id)
            )
        )
        previous = candles[-2] if len(candles) > 1 else None
        signal = detect_structure_break(
            candle, previous, swings, settings.wick_break_allowed
        )
        structure_event = None
        if (
            signal
            and float(signal.confidence) >= settings.structure_confidence_threshold
        ):
            structure_event = db.scalar(
                select(MarketStructureEvent).where(
                    MarketStructureEvent.event_type == signal.event_type,
                    MarketStructureEvent.broken_swing_id == signal.broken_swing.id,
                    MarketStructureEvent.confirmation_candle_id == candle.id,
                )
            )
            if not structure_event:
                structure_event = MarketStructureEvent(
                    symbol_id=candle.symbol_id,
                    timeframe=candle.timeframe,
                    event_type=signal.event_type,
                    direction=signal.direction,
                    broken_swing_id=signal.broken_swing.id,
                    confirmation_candle_id=candle.id,
                    break_price=candle.close,
                    previous_trend=signal.previous_trend,
                    resulting_trend=signal.resulting_trend,
                    confidence=signal.confidence,
                    metadata_json={"wick_break_allowed": settings.wick_break_allowed},
                    detected_at=candle.close_time,
                )
                db.add(structure_event)
                db.flush()
                events.append((signal.event_type.lower(), serialize(structure_event)))
            add_alert(
                db,
                events,
                signal.event_type,
                candle.symbol_id,
                candle.timeframe,
                f"New {signal.direction} {signal.event_type}",
                "structure",
                structure_event.id,
                candle.close_time,
            )
        fvg_signal = detect_fvg(
            candles,
            indicators.get("atr14"),
            indicators.get("volume_ratio"),
            bool(structure_event),
            FVGConfig(
                min_atr_fraction=settings.minimum_fvg_atr_size,
                require_volume_confirmation=settings.fvg_volume_confirmation,
            ),
        )
        if fvg_signal and len(candles) >= 3:
            first, middle, third = candles[-3:]
            zone = db.scalar(
                select(FVGZone).where(
                    FVGZone.first_candle_id == first.id,
                    FVGZone.middle_candle_id == middle.id,
                    FVGZone.third_candle_id == third.id,
                    FVGZone.direction == fvg_signal.direction,
                )
            )
            if not zone:
                zone = FVGZone(
                    symbol_id=candle.symbol_id,
                    timeframe=candle.timeframe,
                    direction=fvg_signal.direction,
                    first_candle_id=first.id,
                    middle_candle_id=middle.id,
                    third_candle_id=third.id,
                    upper_price=fvg_signal.upper_price,
                    lower_price=fvg_signal.lower_price,
                    size_percentage=fvg_signal.size_percentage,
                    status="active",
                    mitigation_percentage=0,
                    detected_at=candle.close_time,
                    metadata_json={"atr_at_detection": indicators.get("atr14")},
                )
                db.add(zone)
                db.flush()
                events.append(("fvg_new", serialize(zone)))
            add_alert(
                db,
                events,
                "NEW_FVG",
                candle.symbol_id,
                candle.timeframe,
                f"New {zone.direction} fair value gap",
                "fvg",
                zone.id,
                candle.close_time,
            )
        active_zones = list(
            db.scalars(
                select(FVGZone).where(
                    FVGZone.symbol_id == candle.symbol_id,
                    FVGZone.timeframe == candle.timeframe,
                    FVGZone.detected_at <= candle.close_time,
                    FVGZone.status.in_(["active", "partially_mitigated"]),
                    FVGZone.third_candle_id != candle.id,
                )
            )
        )
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
        liquidity_signal = detect_liquidity(
            swings, settings.liquidity_tolerance_percentage
        )
        if liquidity_signal:
            pool = db.scalar(
                select(LiquidityPool).where(
                    LiquidityPool.type == liquidity_signal.type,
                    LiquidityPool.first_swing_id == liquidity_signal.first_swing.id,
                    LiquidityPool.second_swing_id == liquidity_signal.second_swing.id,
                )
            )
            if not pool:
                pool = LiquidityPool(
                    symbol_id=candle.symbol_id,
                    timeframe=candle.timeframe,
                    type=liquidity_signal.type,
                    price=liquidity_signal.price,
                    strength=liquidity_signal.strength,
                    first_swing_id=liquidity_signal.first_swing.id,
                    second_swing_id=liquidity_signal.second_swing.id,
                    detected_at=candle.close_time,
                    metadata_json={
                        "pattern": liquidity_signal.pattern,
                        "tolerance_percentage": settings.liquidity_tolerance_percentage,
                    },
                )
                db.add(pool)
                db.flush()
                events.append(("liquidity_new", serialize(pool)))
        htf_snapshot = db.scalar(
            select(AnalysisSnapshot)
            .where(
                AnalysisSnapshot.symbol_id == candle.symbol_id,
                AnalysisSnapshot.timeframe == "4h",
                AnalysisSnapshot.generated_at <= candle.close_time,
            )
            .order_by(AnalysisSnapshot.generated_at.desc())
            .limit(1)
        )
        htf_trend = htf_snapshot.trend if htf_snapshot else "undefined"
        structure_support = bool(structure_event)
        candidates = list(
            db.scalars(
                select(LiquiditySweep).where(
                    LiquiditySweep.symbol_id == candle.symbol_id,
                    LiquiditySweep.timeframe == candle.timeframe,
                    LiquiditySweep.status == "candidate",
                )
            )
        )
        for candidate in candidates:
            pool = db.get(LiquidityPool, candidate.liquidity_pool_id)
            sweep_candle = db.get(Candle, candidate.sweep_candle_id)
            age = (
                db.scalar(
                    select(func.count(Candle.id)).where(
                        Candle.symbol_id == candle.symbol_id,
                        Candle.timeframe == candle.timeframe,
                        Candle.is_closed.is_(True),
                        Candle.open_time > sweep_candle.open_time,
                        Candle.open_time <= candle.open_time,
                    )
                )
                or 0
            )
            decision = update_sweep(
                candidate,
                pool,
                candle,
                age,
                settings,
                indicators.get("volume_ratio"),
                htf_trend == candidate.direction,
                structure_support,
            )
            if decision:
                candidate.status, candidate.sweep_type = (
                    decision.status,
                    decision.sweep_type,
                )
                candidate.extreme_price, candidate.penetration_percentage = (
                    decision.extreme_price,
                    decision.penetration_percentage,
                )
                candidate.rejection_strength, candidate.confidence_score = (
                    decision.rejection_strength,
                    decision.confidence_score,
                )
                candidate.metadata_json = {
                    **candidate.metadata_json,
                    "score_breakdown": decision.score_breakdown,
                }
                if decision.status == "confirmed":
                    (
                        candidate.confirmation_candle_id,
                        candidate.reclaimed_price,
                        candidate.confirmed_at,
                    ) = candle.id, decision.reclaimed_price, candle.close_time
                    pool.status, pool.swept_at = "swept", candle.close_time
                    events.append(("liquidity_sweep_confirmed", serialize(candidate)))
                    add_alert(
                        db,
                        events,
                        "LIQUIDITY_SWEEP_CONFIRMED",
                        candle.symbol_id,
                        candle.timeframe,
                        f"Confirmed {candidate.sweep_type}",
                        "liquidity_sweep",
                        candidate.id,
                        candle.close_time,
                    )
                elif decision.status in {"invalidated", "expired"}:
                    candidate.invalidated_at = candle.close_time
                    pool.status = "invalidated"
                    events.append(("liquidity_sweep_invalidated", serialize(candidate)))
        active_pools = list(
            db.scalars(
                select(LiquidityPool).where(
                    LiquidityPool.symbol_id == candle.symbol_id,
                    LiquidityPool.timeframe == candle.timeframe,
                    LiquidityPool.detected_at <= candle.close_time,
                    LiquidityPool.status == "active",
                )
            )
        )
        for pool in active_pools:
            existing_sweep = db.scalar(
                select(LiquiditySweep.id).where(
                    LiquiditySweep.liquidity_pool_id == pool.id
                )
            )
            if existing_sweep:
                continue
            decision = detect_sweep(
                pool,
                candle,
                settings,
                indicators.get("volume_ratio"),
                htf_trend == ("bullish" if pool.type == "SSL" else "bearish"),
                structure_support,
            )
            if not decision:
                continue
            sweep = LiquiditySweep(
                symbol_id=candle.symbol_id,
                timeframe=candle.timeframe,
                liquidity_pool_id=pool.id,
                direction=decision.direction,
                sweep_type=decision.sweep_type,
                sweep_candle_id=candle.id,
                confirmation_candle_id=candle.id
                if decision.status == "confirmed"
                else None,
                liquidity_price=pool.price,
                extreme_price=decision.extreme_price,
                reclaimed_price=decision.reclaimed_price,
                penetration_percentage=decision.penetration_percentage,
                rejection_strength=decision.rejection_strength,
                volume_ratio=indicators.get("volume_ratio"),
                status=decision.status,
                confidence_score=decision.confidence_score,
                detected_at=candle.close_time,
                confirmed_at=candle.close_time
                if decision.status == "confirmed"
                else None,
                invalidated_at=candle.close_time
                if decision.status == "invalidated"
                else None,
                metadata_json={
                    "score_breakdown": decision.score_breakdown,
                    "supporting_structure_event_id": structure_event.id
                    if structure_event
                    else None,
                },
            )
            db.add(sweep)
            db.flush()
            event_name = (
                "liquidity_sweep_confirmed"
                if decision.status == "confirmed"
                else "liquidity_sweep_invalidated"
                if decision.status == "invalidated"
                else "liquidity_sweep_candidate"
            )
            events.append((event_name, serialize(sweep)))
            if decision.status == "confirmed":
                pool.status, pool.swept_at = "swept", candle.close_time
                add_alert(
                    db,
                    events,
                    "LIQUIDITY_SWEEP_CONFIRMED",
                    candle.symbol_id,
                    candle.timeframe,
                    f"Confirmed {sweep.sweep_type}",
                    "liquidity_sweep",
                    sweep.id,
                    candle.close_time,
                )
            elif decision.status == "invalidated":
                pool.status = "invalidated"
        block_signal = detect_order_block(candles, structure_event)
        if block_signal and structure_event:
            block = db.scalar(
                select(OrderBlock).where(OrderBlock.bos_event_id == structure_event.id)
            )
            if not block:
                block = OrderBlock(
                    symbol_id=candle.symbol_id,
                    timeframe=candle.timeframe,
                    direction=block_signal.direction,
                    candle_id=block_signal.candle.id,
                    top_price=block_signal.top_price,
                    bottom_price=block_signal.bottom_price,
                    bos_event_id=structure_event.id,
                    status="active",
                    mitigation_percent=0,
                    detected_at=candle.close_time,
                )
                db.add(block)
                db.flush()
                events.append(("order_block_new", serialize(block)))
        active_blocks = list(
            db.scalars(
                select(OrderBlock).where(
                    OrderBlock.symbol_id == candle.symbol_id,
                    OrderBlock.timeframe == candle.timeframe,
                    OrderBlock.detected_at <= candle.close_time,
                    OrderBlock.status.in_(["active", "partially_mitigated"]),
                    OrderBlock.candle_id != candle.id,
                )
            )
        )
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
                add_alert(
                    db,
                    events,
                    "ORDER_BLOCK_MITIGATION",
                    candle.symbol_id,
                    candle.timeframe,
                    f"{block.direction} order block {status}",
                    "order_block",
                    block.id,
                    candle.close_time,
                )
        active_fvgs = list(
            db.scalars(
                select(FVGZone).where(
                    FVGZone.symbol_id == candle.symbol_id,
                    FVGZone.timeframe == candle.timeframe,
                    FVGZone.detected_at <= candle.close_time,
                )
            )
        )
        all_blocks = list(
            db.scalars(
                select(OrderBlock).where(
                    OrderBlock.symbol_id == candle.symbol_id,
                    OrderBlock.timeframe == candle.timeframe,
                    OrderBlock.detected_at <= candle.close_time,
                )
            )
        )
        confirmed_sweep = db.scalar(
            select(LiquiditySweep)
            .where(
                LiquiditySweep.symbol_id == candle.symbol_id,
                LiquiditySweep.timeframe == candle.timeframe,
                LiquiditySweep.status == "confirmed",
                LiquiditySweep.confirmed_at <= candle.close_time,
            )
            .order_by(LiquiditySweep.confirmed_at.desc())
            .limit(1)
        )
        wave_changes, primary_wave = process_elliott_candidates(
            db,
            candle,
            swings,
            settings,
            {
                "structure": bool(structure_event),
                "higher_timeframe": htf_trend,
                "liquidity": bool(confirmed_sweep),
                "zone": bool(active_fvgs or all_blocks),
                "momentum": indicators.get("volume_ratio") is not None
                and indicators.get("volume_ratio", 0) >= 1,
                "atr": indicators.get("atr14"),
                "structure_event_id": structure_event.id if structure_event else None,
                "liquidity_sweep_id": confirmed_sweep.id if confirmed_sweep else None,
                "fvg_zone_ids": [zone.id for zone in active_fvgs],
                "order_block_ids": [block.id for block in all_blocks],
            },
        )
        for wave in wave_changes:
            events.append(("elliott_wave_updated", serialize(wave)))
        setup_structure = structure_event or db.scalar(
            select(MarketStructureEvent)
            .where(
                MarketStructureEvent.symbol_id == candle.symbol_id,
                MarketStructureEvent.timeframe == candle.timeframe,
                MarketStructureEvent.detected_at <= candle.close_time,
            )
            .order_by(MarketStructureEvent.detected_at.desc())
            .limit(1)
        )
        if setup_structure and setup_structure.event_type in {"BOS", "CHoCH"}:
            direction = setup_structure.direction
            applicable_sweep = (
                confirmed_sweep
                if confirmed_sweep
                and confirmed_sweep.direction == direction
                and confirmed_sweep.confirmed_at <= setup_structure.detected_at
                else None
            )
            continuation = (
                applicable_sweep is None and setup_structure.event_type == "BOS"
            )
            if applicable_sweep or continuation:
                existing_setup = db.scalar(
                    select(TradeSetup).where(
                        TradeSetup.strategy
                        == f"{direction}_{'continuation' if continuation else 'liquidity_reversal'}",
                        TradeSetup.structure_event_id == setup_structure.id,
                        TradeSetup.setup_timeframe == candle.timeframe,
                    )
                )
                if not existing_setup and (active_fvgs or all_blocks):
                    decision = generate_setup(
                        direction,
                        setup_structure,
                        candle,
                        settings,
                        active_fvgs,
                        all_blocks,
                        swings,
                        active_pools,
                        indicators,
                        htf_trend,
                        premium_discount(swings),
                        applicable_sweep,
                        continuation,
                    )
                    setup = TradeSetup(
                        symbol_id=candle.symbol_id,
                        direction=decision.direction,
                        strategy=decision.strategy,
                        status=decision.status,
                        higher_timeframe="4h",
                        setup_timeframe=candle.timeframe,
                        entry_timeframe="15m",
                        liquidity_sweep_id=applicable_sweep.id
                        if applicable_sweep
                        else None,
                        structure_event_id=setup_structure.id,
                        fvg_zone_id=decision.fvg.id if decision.fvg else None,
                        order_block_id=decision.order_block.id
                        if decision.order_block
                        else None,
                        entry_min=decision.entry_min,
                        entry_max=decision.entry_max,
                        preferred_entry=decision.preferred_entry,
                        stop_loss=decision.stop_loss,
                        invalidation_price=decision.stop_loss,
                        take_profit_1=decision.targets[0],
                        take_profit_2=decision.targets[1],
                        take_profit_3=decision.targets[2],
                        risk_reward_1=decision.risk_rewards[0],
                        risk_reward_2=decision.risk_rewards[1],
                        risk_reward_3=decision.risk_rewards[2],
                        confidence_score=decision.confidence_score,
                        score_breakdown_json=decision.score_breakdown,
                        setup_conditions_json=decision.conditions,
                        rejection_reasons_json=decision.rejection_reasons,
                        expires_at=decision.expires_at,
                        detected_at=candle.close_time,
                    )
                    db.add(setup)
                    db.flush()
                    events.append(("trade_setup_created", serialize(setup)))
                    if setup.status == "ready":
                        events.append(("trade_setup_ready", serialize(setup)))
                        add_alert(
                            db,
                            events,
                            "TRADE_SETUP_READY",
                            candle.symbol_id,
                            candle.timeframe,
                            f"{setup.strategy} ready",
                            "trade_setup",
                            setup.id,
                            candle.close_time,
                        )
        wave_structure_types = set()
        if primary_wave:
            wave_structure_types = set(
                db.scalars(
                    select(MarketStructureEvent.event_type)
                    .where(
                        MarketStructureEvent.symbol_id == candle.symbol_id,
                        MarketStructureEvent.timeframe == candle.timeframe,
                        MarketStructureEvent.direction == primary_wave.direction,
                        MarketStructureEvent.detected_at <= candle.close_time,
                    )
                    .order_by(MarketStructureEvent.detected_at.desc())
                    .limit(20)
                )
            )
        if primary_wave and setup_structure and active_fvgs + all_blocks:
            wave_label = primary_wave.metadata_json.get("current_wave")
            wave_strategy = None
            wave_sweep = None
            aligned_sweep = bool(
                confirmed_sweep and confirmed_sweep.direction == primary_wave.direction
            )
            if setup_structure.direction == primary_wave.direction:
                wave_strategy = select_wave_strategy(
                    wave_label,
                    primary_wave.direction,
                    aligned_sweep,
                    wave_structure_types,
                )
                if wave_label in {"2", "B"} and aligned_sweep:
                    wave_sweep = confirmed_sweep
            if wave_strategy:
                existing_wave_setup = db.scalar(
                    select(TradeSetup).where(
                        TradeSetup.strategy == wave_strategy,
                        TradeSetup.structure_event_id == setup_structure.id,
                        TradeSetup.setup_timeframe == candle.timeframe,
                    )
                )
                if not existing_wave_setup:
                    decision = generate_setup(
                        primary_wave.direction,
                        setup_structure,
                        candle,
                        settings,
                        active_fvgs,
                        all_blocks,
                        swings,
                        active_pools,
                        indicators,
                        htf_trend,
                        premium_discount(swings),
                        wave_sweep,
                        wave_sweep is None,
                    )
                    preferred, stop = decision.preferred_entry, decision.stop_loss
                    targets = list(decision.targets)
                    if (
                        preferred is not None
                        and stop is not None
                        and primary_wave.projected_target_min is not None
                    ):
                        targets[1], targets[2] = (
                            (
                                primary_wave.projected_target_min,
                                primary_wave.projected_target_max,
                            )
                            if primary_wave.direction == "bullish"
                            else (
                                primary_wave.projected_target_max,
                                primary_wave.projected_target_min,
                            )
                        )
                    risk = (
                        abs(preferred - stop)
                        if preferred is not None and stop is not None
                        else Decimal("0")
                    )
                    rrs = [
                        None
                        if risk <= 0 or target is None
                        else abs(target - preferred) / risk
                        for target in targets
                    ]
                    status = decision.status
                    wave_risk_factor = (
                        settings.elliott_wave_5_risk_factor if wave_label == "4" else 1
                    )
                    setup = TradeSetup(
                        symbol_id=candle.symbol_id,
                        direction=primary_wave.direction,
                        strategy=wave_strategy,
                        status=status,
                        higher_timeframe="4h",
                        setup_timeframe=candle.timeframe,
                        entry_timeframe="15m",
                        liquidity_sweep_id=wave_sweep.id if wave_sweep else None,
                        structure_event_id=setup_structure.id,
                        fvg_zone_id=decision.fvg.id if decision.fvg else None,
                        order_block_id=decision.order_block.id
                        if decision.order_block
                        else None,
                        elliott_wave_count_id=primary_wave.id,
                        entry_min=decision.entry_min,
                        entry_max=decision.entry_max,
                        preferred_entry=preferred,
                        stop_loss=stop,
                        invalidation_price=stop,
                        take_profit_1=targets[0],
                        take_profit_2=targets[1],
                        take_profit_3=targets[2],
                        risk_reward_1=rrs[0],
                        risk_reward_2=rrs[1],
                        risk_reward_3=rrs[2],
                        confidence_score=decision.confidence_score,
                        score_breakdown_json={
                            **decision.score_breakdown,
                            "elliott_wave_confluence": float(
                                primary_wave.confidence_score
                            ),
                        },
                        setup_conditions_json={
                            **decision.conditions,
                            "wave": wave_label,
                            "wave_count_id": primary_wave.id,
                            "risk_factor": wave_risk_factor,
                        },
                        rejection_reasons_json=decision.rejection_reasons,
                        expires_at=decision.expires_at,
                        detected_at=candle.close_time,
                    )
                    db.add(setup)
                    db.flush()
                    events.append(("trade_setup_created", serialize(setup)))
        live_setups = list(
            db.scalars(
                select(TradeSetup).where(
                    TradeSetup.symbol_id == candle.symbol_id,
                    TradeSetup.setup_timeframe == candle.timeframe,
                    TradeSetup.status.in_(["watching", "ready", "triggered"]),
                    TradeSetup.detected_at < candle.close_time,
                )
            )
        )
        for setup in live_setups:
            supporting_fvg = (
                db.get(FVGZone, setup.fvg_zone_id) if setup.fvg_zone_id else None
            )
            supporting_block = (
                db.get(OrderBlock, setup.order_block_id)
                if setup.order_block_id
                else None
            )
            support_invalid = (
                supporting_fvg and supporting_fvg.status == "invalidated"
            ) or (supporting_block and supporting_block.status == "invalidated")
            next_status = (
                "invalidated"
                if support_invalid
                else update_setup_lifecycle(setup, candle)
            )
            if not next_status:
                continue
            setup.status = next_status
            if next_status == "triggered":
                setup.triggered_at = candle.close_time
            if next_status == "invalidated":
                setup.invalidated_at = candle.close_time
            events.append((f"trade_setup_{next_status}", serialize(setup)))
            add_alert(
                db,
                events,
                f"TRADE_SETUP_{next_status.upper()}",
                candle.symbol_id,
                candle.timeframe,
                f"{setup.strategy} {next_status}",
                "trade_setup",
                setup.id,
                candle.close_time,
            )
        trend = classify_trend(swings)
        latest_event = db.scalar(
            select(MarketStructureEvent)
            .where(
                MarketStructureEvent.symbol_id == candle.symbol_id,
                MarketStructureEvent.timeframe == candle.timeframe,
                MarketStructureEvent.detected_at <= candle.close_time,
            )
            .order_by(
                MarketStructureEvent.detected_at.desc(), MarketStructureEvent.id.desc()
            )
            .limit(1)
        )
        active_count = (
            db.scalar(
                select(func.count(FVGZone.id)).where(
                    FVGZone.symbol_id == candle.symbol_id,
                    FVGZone.timeframe == candle.timeframe,
                    FVGZone.detected_at <= candle.close_time,
                    FVGZone.status.in_(["active", "partially_mitigated"]),
                )
            )
            or 0
        )
        snapshot = db.scalar(
            select(AnalysisSnapshot).where(
                AnalysisSnapshot.symbol_id == candle.symbol_id,
                AnalysisSnapshot.timeframe == candle.timeframe,
                AnalysisSnapshot.generated_at == candle.close_time,
            )
        )
        if not snapshot:
            snapshot = AnalysisSnapshot(
                symbol_id=candle.symbol_id,
                timeframe=candle.timeframe,
                trend=trend,
                latest_structure_event=latest_event.event_type
                if latest_event
                else None,
                active_fvg_count=active_count,
                indicator_values_json=indicators,
                confidence_score=latest_event.confidence
                if latest_event
                else Decimal("0.5"),
                generated_at=candle.close_time,
            )
            db.add(snapshot)
            db.flush()
            events.append(("analysis_snapshot", serialize(snapshot)))
        elif snapshot.indicator_values_json.get("_smc_version") != 4:
            snapshot.indicator_values_json = indicators
        db.commit()
        response = {"processed": True, "candle_id": candle_id, "events": len(events)}
    if broadcast:
        for event_type, payload in events:
            await broadcaster.broadcast(event_type, payload)
    return response
