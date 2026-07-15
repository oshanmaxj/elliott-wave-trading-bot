import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select

from app.core.logging import log_event
from app.database.session import SessionLocal
from app.models import (
    Alert,
    AnalysisSnapshot,
    Candle,
    ElliottWaveCount,
    ElliottWavePoint,
    FVGZone,
    LiquidityPool,
    LiquiditySweep,
    MarketStructureEvent,
    OrderBlock,
    SwingPoint,
    Symbol,
    TradeSetup,
)
from app.services.broadcast import broadcaster
from app.services.pipeline import process_closed_candle


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AnalysisBackfillStatus:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.running = False
        self.symbol: str | None = None
        self.timeframe: str | None = None
        self.total_candles = 0
        self.processed_candles = 0
        self.failed_candles = 0
        self.started_at: datetime | None = None
        self.last_completed_at: datetime | None = None

    def start(
        self, symbol: str, timeframe: str, total: int, started_at: datetime
    ) -> None:
        self.running = True
        self.symbol = symbol
        self.timeframe = timeframe
        self.total_candles = total
        self.processed_candles = 0
        self.failed_candles = 0
        self.started_at = started_at

    def finish(self, completed_at: datetime) -> None:
        self.running = False
        self.last_completed_at = completed_at

    def report(self) -> dict[str, Any]:
        completed = self.processed_candles + self.failed_candles
        progress = (
            0.0
            if self.total_candles == 0 and self.running
            else (
                100.0
                if self.total_candles == 0
                else round(completed / self.total_candles * 100, 2)
            )
        )
        return {
            "running": self.running,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "total_candles": self.total_candles,
            "processed_candles": self.processed_candles,
            "failed_candles": self.failed_candles,
            "progress_percentage": progress,
            "started_at": self.started_at,
            "last_completed_at": self.last_completed_at,
        }


backfill_status = AnalysisBackfillStatus()


class AnalysisBackfillService:
    def __init__(
        self,
        session_factory=SessionLocal,
        processor: Callable[..., Any] = process_closed_candle,
    ):
        self.session_factory = session_factory
        self.processor = processor

    def _candle_ids(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime | None,
        end_time: datetime | None,
        limit: int | None,
    ) -> tuple[int | None, list[int]]:
        with self.session_factory() as db:
            symbol_id = db.scalar(select(Symbol.id).where(Symbol.symbol == symbol))
            if symbol_id is None:
                return None, []
            query = select(Candle.id).where(
                Candle.symbol_id == symbol_id,
                Candle.timeframe == timeframe,
                Candle.is_closed.is_(True),
            )
            if start_time is not None:
                query = query.where(Candle.open_time >= start_time)
            if end_time is not None:
                query = query.where(Candle.open_time <= end_time)
            query = query.order_by(Candle.open_time.asc(), Candle.id.asc())
            if limit is not None:
                query = query.limit(limit)
            return symbol_id, list(db.scalars(query))

    def _delete_derived(self, symbol_id: int, timeframe: str) -> None:
        def filters(model):
            return model.symbol_id == symbol_id, model.timeframe == timeframe

        with self.session_factory.begin() as db:
            db.execute(delete(Alert).where(*filters(Alert)))
            db.execute(delete(AnalysisSnapshot).where(*filters(AnalysisSnapshot)))
            db.execute(
                delete(TradeSetup).where(
                    TradeSetup.symbol_id == symbol_id,
                    TradeSetup.setup_timeframe == timeframe,
                )
            )
            count_ids = select(ElliottWaveCount.id).where(
                ElliottWaveCount.symbol_id == symbol_id,
                ElliottWaveCount.timeframe == timeframe,
            )
            db.execute(
                delete(ElliottWavePoint).where(
                    ElliottWavePoint.wave_count_id.in_(count_ids)
                )
            )
            db.execute(
                delete(ElliottWaveCount).where(
                    ElliottWaveCount.symbol_id == symbol_id,
                    ElliottWaveCount.timeframe == timeframe,
                )
            )
            db.execute(delete(LiquiditySweep).where(*filters(LiquiditySweep)))
            db.execute(delete(OrderBlock).where(*filters(OrderBlock)))
            db.execute(delete(LiquidityPool).where(*filters(LiquidityPool)))
            db.execute(
                delete(MarketStructureEvent).where(*filters(MarketStructureEvent))
            )
            db.execute(delete(FVGZone).where(*filters(FVGZone)))
            db.execute(delete(SwingPoint).where(*filters(SwingPoint)))

    async def run(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = None,
        rebuild: bool = False,
    ) -> dict[str, Any]:
        async with backfill_status.lock:
            started_at = _utcnow()
            started_perf = time.perf_counter()
            symbol_id, candle_ids = self._candle_ids(
                symbol, timeframe, start_time, end_time, limit
            )
            backfill_status.start(symbol, timeframe, len(candle_ids), started_at)
            processed = skipped = failed = events_generated = 0
            try:
                if rebuild and symbol_id is not None:
                    self._delete_derived(symbol_id, timeframe)
                for candle_id in candle_ids:
                    if not rebuild:
                        with self.session_factory() as db:
                            existing_snapshot = db.scalar(
                                select(AnalysisSnapshot)
                                .join(
                                    Candle,
                                    AnalysisSnapshot.symbol_id == Candle.symbol_id,
                                )
                                .where(
                                    Candle.id == candle_id,
                                    AnalysisSnapshot.timeframe == timeframe,
                                    AnalysisSnapshot.generated_at == Candle.close_time,
                                )
                            )
                        if (
                            existing_snapshot is not None
                            and existing_snapshot.indicator_values_json.get(
                                "_smc_version"
                            )
                            == 4
                        ):
                            skipped += 1
                            backfill_status.processed_candles += 1
                            continue
                    try:
                        result = await self.processor(
                            candle_id,
                            broadcast=False,
                            session_factory=self.session_factory,
                        )
                        if result.get("processed"):
                            processed += 1
                            events_generated += int(result.get("events", 0))
                        else:
                            skipped += 1
                        backfill_status.processed_candles += 1
                    except Exception as exc:
                        failed += 1
                        backfill_status.failed_candles += 1
                        log_event(
                            "ERROR",
                            "analysis_backfill",
                            "candle_failed",
                            str(exc),
                            {
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "candle_id": candle_id,
                            },
                        )
                completed_at = _utcnow()
                report = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "total_candles": len(candle_ids),
                    "processed": processed,
                    "skipped": skipped,
                    "failed": failed,
                    "events_generated": events_generated,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "duration_ms": round(
                        (time.perf_counter() - started_perf) * 1000, 2
                    ),
                }
                log_event(
                    "INFO",
                    "analysis_backfill",
                    "backfill_complete",
                    "Historical analysis backfill completed",
                    report,
                )
                await broadcaster.broadcast(
                    "analysis_backfill_completed", jsonable_encoder(report)
                )
                return report
            finally:
                backfill_status.finish(_utcnow())
