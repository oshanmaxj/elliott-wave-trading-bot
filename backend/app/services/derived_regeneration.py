"""Range-scoped deterministic regeneration of candle-derived analysis data."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select

from app.database.session import SessionLocal
from app.models import (
    Alert,
    AnalysisSnapshot,
    Candle,
    ElliottWaveCount,
    ElliottWavePoint,
    ExecutionEvent,
    ExecutionOrder,
    FVGZone,
    LiquidityPool,
    LiquiditySweep,
    LivePosition,
    MarketStructureEvent,
    OrderBlock,
    SwingPoint,
    Symbol,
    TradeSetup,
)
from app.services.pipeline import process_closed_candle


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DerivedRegenerationService:
    def __init__(self, session_factory=SessionLocal, processor=process_closed_candle):
        self.session_factory = session_factory
        self.processor = processor

    def inspect(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> dict[str, Any]:
        start, end = utc(start), utc(end)
        if end <= start:
            raise ValueError("end must be after start")
        with self.session_factory() as db:
            symbol_id = db.scalar(select(Symbol.id).where(Symbol.symbol == symbol.upper()))
            if symbol_id is None:
                raise ValueError(f"unknown symbol {symbol}")
            candle_ids = list(db.scalars(select(Candle.id).where(
                Candle.symbol_id == symbol_id, Candle.timeframe == timeframe,
                Candle.is_closed.is_(True), Candle.open_time >= start, Candle.open_time < end,
            ).order_by(Candle.open_time, Candle.id)))
            swing_ids = list(db.scalars(select(SwingPoint.id).where(
                SwingPoint.symbol_id == symbol_id, SwingPoint.timeframe == timeframe,
                or_(SwingPoint.candle_id.in_(candle_ids), SwingPoint.detected_at >= start),
                SwingPoint.detected_at < end,
            )))
            structure_ids = list(db.scalars(select(MarketStructureEvent.id).where(
                MarketStructureEvent.symbol_id == symbol_id,
                MarketStructureEvent.timeframe == timeframe,
                or_(MarketStructureEvent.confirmation_candle_id.in_(candle_ids),
                    MarketStructureEvent.broken_swing_id.in_(swing_ids),
                    MarketStructureEvent.detected_at >= start),
                MarketStructureEvent.detected_at < end,
            )))
            wave_ids = list(db.scalars(select(ElliottWaveCount.id).where(
                ElliottWaveCount.symbol_id == symbol_id,
                ElliottWaveCount.timeframe == timeframe,
                ElliottWaveCount.detected_at >= start, ElliottWaveCount.detected_at < end,
            )))
            setup_ids = list(db.scalars(select(TradeSetup.id).where(
                TradeSetup.symbol_id == symbol_id, TradeSetup.setup_timeframe == timeframe,
                or_(TradeSetup.detected_at.between(start, end),
                    TradeSetup.structure_event_id.in_(structure_ids),
                    TradeSetup.elliott_wave_count_id.in_(wave_ids)),
            )))
            protected_setup_ids = sorted(set(db.scalars(
                select(TradeSetup.id).where(TradeSetup.id.in_(setup_ids)).where(or_(
                    TradeSetup.id.in_(select(ExecutionOrder.trade_setup_id)),
                    TradeSetup.id.in_(select(LivePosition.originating_trade_setup_id)),
                    TradeSetup.id.in_(select(ExecutionEvent.trade_setup_id).where(ExecutionEvent.trade_setup_id.is_not(None))),
                ))
            )))
            counts = {
                "candles_to_replay": len(candle_ids),
                "swing_points": len(swing_ids),
                "market_structure_events": len(structure_ids),
                "fvg_zones": db.scalar(select(func.count(FVGZone.id)).where(FVGZone.symbol_id == symbol_id, FVGZone.timeframe == timeframe, FVGZone.detected_at >= start, FVGZone.detected_at < end)) or 0,
                "liquidity_pools": db.scalar(select(func.count(LiquidityPool.id)).where(LiquidityPool.symbol_id == symbol_id, LiquidityPool.timeframe == timeframe, LiquidityPool.detected_at >= start, LiquidityPool.detected_at < end)) or 0,
                "liquidity_sweeps": db.scalar(select(func.count(LiquiditySweep.id)).where(LiquiditySweep.symbol_id == symbol_id, LiquiditySweep.timeframe == timeframe, LiquiditySweep.detected_at >= start, LiquiditySweep.detected_at < end)) or 0,
                "order_blocks": db.scalar(select(func.count(OrderBlock.id)).where(OrderBlock.symbol_id == symbol_id, OrderBlock.timeframe == timeframe, OrderBlock.detected_at >= start, OrderBlock.detected_at < end)) or 0,
                "elliott_wave_counts": len(wave_ids),
                "trade_setups": len(setup_ids),
                "analysis_snapshots": db.scalar(select(func.count(AnalysisSnapshot.id)).where(AnalysisSnapshot.symbol_id == symbol_id, AnalysisSnapshot.timeframe == timeframe, AnalysisSnapshot.generated_at >= start, AnalysisSnapshot.generated_at < end)) or 0,
            }
            return {"symbol": symbol.upper(), "symbol_id": symbol_id, "timeframe": timeframe,
                    "start": start, "end": end, "apply": False, "counts": counts,
                    "protected_setup_ids": protected_setup_ids, "candle_ids": candle_ids,
                    "swing_ids": swing_ids, "structure_ids": structure_ids,
                    "wave_ids": wave_ids, "setup_ids": setup_ids}

    async def run(self, symbol: str, timeframe: str, start: datetime, end: datetime, apply: bool = False) -> dict[str, Any]:
        report = self.inspect(symbol, timeframe, start, end)
        if not apply:
            return report
        if report["protected_setup_ids"]:
            raise RuntimeError(
                "regeneration intersects execution-audit trade setups; refusing mutation: "
                + ",".join(map(str, report["protected_setup_ids"]))
            )
        symbol_id, start, end = report["symbol_id"], report["start"], report["end"]
        with self.session_factory.begin() as db:
            db.execute(delete(Alert).where(Alert.symbol_id == symbol_id, Alert.timeframe == timeframe, Alert.created_at >= start, Alert.created_at < end))
            db.execute(delete(AnalysisSnapshot).where(AnalysisSnapshot.symbol_id == symbol_id, AnalysisSnapshot.timeframe == timeframe, AnalysisSnapshot.generated_at >= start, AnalysisSnapshot.generated_at < end))
            db.execute(delete(TradeSetup).where(TradeSetup.id.in_(report["setup_ids"])))
            db.execute(delete(ElliottWavePoint).where(ElliottWavePoint.wave_count_id.in_(report["wave_ids"])))
            db.execute(delete(ElliottWaveCount).where(ElliottWaveCount.id.in_(report["wave_ids"])))
            db.execute(delete(LiquiditySweep).where(LiquiditySweep.symbol_id == symbol_id, LiquiditySweep.timeframe == timeframe, LiquiditySweep.detected_at >= start, LiquiditySweep.detected_at < end))
            db.execute(delete(OrderBlock).where(OrderBlock.symbol_id == symbol_id, OrderBlock.timeframe == timeframe, OrderBlock.detected_at >= start, OrderBlock.detected_at < end))
            db.execute(delete(LiquidityPool).where(LiquidityPool.symbol_id == symbol_id, LiquidityPool.timeframe == timeframe, LiquidityPool.detected_at >= start, LiquidityPool.detected_at < end))
            db.execute(delete(MarketStructureEvent).where(MarketStructureEvent.id.in_(report["structure_ids"])))
            db.execute(delete(FVGZone).where(FVGZone.symbol_id == symbol_id, FVGZone.timeframe == timeframe, FVGZone.detected_at >= start, FVGZone.detected_at < end))
            db.execute(delete(SwingPoint).where(SwingPoint.id.in_(report["swing_ids"])))
        processed = failed = 0
        errors = []
        for candle_id in report["candle_ids"]:
            try:
                result = await self.processor(candle_id, broadcast=False, session_factory=self.session_factory, allow_trading_side_effects=False)
                processed += int(bool(result.get("processed")))
            except Exception as exc:
                failed += 1
                errors.append({"candle_id": candle_id, "error": str(exc)})
        return {**report, "apply": True, "processed": processed, "failed": failed,
                "errors": errors, "post_regeneration": self.inspect(symbol, timeframe, start, end)["counts"]}
