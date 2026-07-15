from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select

from app.core.constants import TIMEFRAME_MS
from app.database.session import SessionLocal
from app.elliott.engine import assign_degree, generate_candidates
from app.models import (
    Candle,
    ElliottWaveCount,
    ElliottWavePoint,
    SwingPoint,
    Symbol,
    TradeSetup,
)
from app.services.settings import get_runtime_settings


def process_elliott_candidates(
    db,
    candle: Candle,
    swings: list[Any],
    settings: Any,
    context: dict[str, Any] | None = None,
) -> tuple[list[ElliottWaveCount], ElliottWaveCount | None]:
    changed: list[ElliottWaveCount] = []
    active = list(
        db.scalars(
            select(ElliottWaveCount).where(
                ElliottWaveCount.symbol_id == candle.symbol_id,
                ElliottWaveCount.timeframe == candle.timeframe,
                ElliottWaveCount.status.in_(
                    ["candidate", "primary", "alternate", "confirmed"]
                ),
            )
        )
    )
    for count in active:
        crossed = (
            count.direction == "bullish" and candle.low < count.invalidation_price
        ) or (count.direction == "bearish" and candle.high > count.invalidation_price)
        if crossed:
            count.status, count.invalidated_at = "invalidated", candle.close_time
            changed.append(count)
    candidates = generate_candidates(
        swings,
        settings.elliott_fibonacci_tolerance,
        settings.elliott_allow_zigzag_truncation,
        context,
    )
    for candidate in candidates:
        if candidate.confidence_score < Decimal(
            str(settings.elliott_minimum_confidence)
        ):
            continue
        first, last = candidate.swings[0], candidate.swings[-1]
        row = db.scalar(
            select(ElliottWaveCount).where(
                ElliottWaveCount.symbol_id == candle.symbol_id,
                ElliottWaveCount.timeframe == candle.timeframe,
                ElliottWaveCount.pattern_type == candidate.pattern_type,
                ElliottWaveCount.start_candle_id == first.candle.id,
                ElliottWaveCount.end_candle_id == last.candle.id,
            )
        )
        if row:
            continue
        row = ElliottWaveCount(
            symbol_id=candle.symbol_id,
            timeframe=candle.timeframe,
            degree=assign_degree(
                candle.timeframe,
                list(candidate.swings),
                context.get("atr") if context else None,
            ),
            direction=candidate.direction,
            pattern_type=candidate.pattern_type,
            status="candidate",
            rank=0,
            confidence_score=candidate.confidence_score,
            start_candle_id=first.candle.id,
            end_candle_id=last.candle.id,
            invalidation_price=candidate.invalidation_price,
            projected_target_min=candidate.target_min,
            projected_target_max=candidate.target_max,
            rules_passed_json=list(candidate.rules_passed),
            rules_failed_json=list(candidate.rules_failed),
            fibonacci_scores_json=candidate.fibonacci,
            structure_confirmation_json={
                "aligned": bool(context and context.get("structure")),
                "event_id": context.get("structure_event_id") if context else None,
            },
            liquidity_confirmation_json={
                "sweep_confirmed": bool(context and context.get("liquidity")),
                "sweep_id": context.get("liquidity_sweep_id") if context else None,
            },
            metadata_json={
                "phase": candidate.phase,
                "current_wave": candidate.labels[-1],
                "point_count": len(candidate.labels),
                "fvg_zone_ids": context.get("fvg_zone_ids", []) if context else [],
                "order_block_ids": context.get("order_block_ids", [])
                if context
                else [],
            },
            detected_at=candle.close_time,
            confirmed_at=candle.close_time if candidate.phase == "complete" else None,
            completed_at=candle.close_time if candidate.phase == "complete" else None,
        )
        db.add(row)
        db.flush()
        step_ms = TIMEFRAME_MS[candle.timeframe]
        ratios = list(candidate.fibonacci.values())
        for index, (label, swing) in enumerate(zip(candidate.labels, candidate.swings)):
            duration = (
                0
                if index == 0
                else max(
                    1,
                    round(
                        (
                            swing.candle.open_time
                            - candidate.swings[index - 1].candle.open_time
                        ).total_seconds()
                        * 1000
                        / step_ms
                    ),
                )
            )
            ratio = (
                None
                if index == 0 or index - 1 >= len(ratios)
                else ratios[index - 1].get("actual")
            )
            db.add(
                ElliottWavePoint(
                    wave_count_id=row.id,
                    wave_label=label,
                    sequence_number=index,
                    swing_point_id=swing.id,
                    candle_id=swing.candle.id,
                    price=swing.price,
                    timestamp=swing.candle.open_time,
                    fibonacci_ratio=ratio,
                    duration_bars=duration,
                    metadata_json={
                        "swing_type": swing.swing_type,
                        "strength": str(swing.strength),
                    },
                )
            )
        changed.append(row)
    db.flush()
    cutoff = candle.close_time - timedelta(
        milliseconds=TIMEFRAME_MS[candle.timeframe] * 100
    )
    stale = list(
        db.scalars(
            select(ElliottWaveCount).where(
                ElliottWaveCount.symbol_id == candle.symbol_id,
                ElliottWaveCount.timeframe == candle.timeframe,
                ElliottWaveCount.status.in_(["primary", "alternate", "confirmed"]),
                ElliottWaveCount.detected_at < cutoff,
            )
        )
    )
    for row in stale:
        row.status = (
            "completed" if row.metadata_json.get("phase") == "complete" else "candidate"
        )
        row.rank = 0
        if row not in changed:
            changed.append(row)
    rankable = list(
        db.scalars(
            select(ElliottWaveCount)
            .where(
                ElliottWaveCount.symbol_id == candle.symbol_id,
                ElliottWaveCount.timeframe == candle.timeframe,
                ElliottWaveCount.status.notin_(["invalidated", "completed"]),
                ElliottWaveCount.detected_at >= cutoff,
            )
            .order_by(
                ElliottWaveCount.confidence_score.desc(),
                ElliottWaveCount.detected_at.desc(),
                ElliottWaveCount.id.desc(),
            )
        )
    )
    for index, row in enumerate(rankable):
        new_status = (
            "primary"
            if index == 0
            else "alternate"
            if index <= settings.elliott_max_alternate_counts
            else "candidate"
        )
        new_rank = index + 1 if index <= settings.elliott_max_alternate_counts else 0
        if row.status != new_status or row.rank != new_rank:
            row.status, row.rank = new_status, new_rank
            if row not in changed:
                changed.append(row)
    return changed, rankable[0] if rankable else None


def recalculate_elliott(
    symbol: str, timeframe: str, rebuild: bool = False, session_factory=SessionLocal
) -> dict[str, Any]:
    with session_factory.begin() as db:
        symbol_id = db.scalar(select(Symbol.id).where(Symbol.symbol == symbol))
        if symbol_id is None:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "processed": 0,
                "counts": 0,
            }
        if rebuild:
            ids = select(ElliottWaveCount.id).where(
                ElliottWaveCount.symbol_id == symbol_id,
                ElliottWaveCount.timeframe == timeframe,
            )
            db.execute(
                delete(TradeSetup).where(TradeSetup.elliott_wave_count_id.in_(ids))
            )
            db.execute(
                delete(ElliottWavePoint).where(ElliottWavePoint.wave_count_id.in_(ids))
            )
            db.execute(
                delete(ElliottWaveCount).where(
                    ElliottWaveCount.symbol_id == symbol_id,
                    ElliottWaveCount.timeframe == timeframe,
                )
            )
        candles = list(
            db.scalars(
                select(Candle)
                .where(
                    Candle.symbol_id == symbol_id,
                    Candle.timeframe == timeframe,
                    Candle.is_closed.is_(True),
                )
                .order_by(Candle.open_time)
            )
        )
        settings = get_runtime_settings(db)
        processed = 0
        for candle in candles:
            swings = list(
                db.scalars(
                    select(SwingPoint)
                    .where(
                        SwingPoint.symbol_id == symbol_id,
                        SwingPoint.timeframe == timeframe,
                        SwingPoint.detected_at <= candle.close_time,
                    )
                    .order_by(SwingPoint.detected_at, SwingPoint.id)
                )
            )
            process_elliott_candidates(db, candle, swings, settings, {})
            processed += 1
        count = len(
            list(
                db.scalars(
                    select(ElliottWaveCount.id).where(
                        ElliottWaveCount.symbol_id == symbol_id,
                        ElliottWaveCount.timeframe == timeframe,
                    )
                )
            )
        )
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "processed": processed,
            "counts": count,
            "rebuild": rebuild,
        }
