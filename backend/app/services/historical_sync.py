from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.constants import TIMEFRAME_MS
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.market_data.binance_rest import BinanceRESTClient
from app.models import Candle, Symbol
from app.repositories.market import ensure_symbol, upsert_candle
from app.services.analysis_backfill import AnalysisBackfillService


class HistoricalSyncService:
    def __init__(self, client: BinanceRESTClient | None = None):
        self.client = client or BinanceRESTClient()

    async def sync(self, symbol: str, timeframe: str, start_time: datetime | None = None, end_time: datetime | None = None) -> dict[str, Any]:
        config = get_settings()
        if start_time is None:
            start_time = (end_time or datetime.now(timezone.utc)) - timedelta(milliseconds=TIMEFRAME_MS[timeframe] * config.historical_candle_limit)
        candles = await self.client.fetch_paginated(symbol, timeframe, start_time, end_time)
        created = updated = 0
        with SessionLocal.begin() as db:
            symbol_row = ensure_symbol(db, symbol)
            for data in candles:
                _, was_created = upsert_candle(db, symbol_row.id, timeframe, data)
                created += int(was_created)
                updated += int(not was_created)
        gaps = self.detect_gaps(symbol, timeframe, start_time, end_time)
        backfilled = 0
        for gap_start, gap_end in gaps:
            missing = await self.client.fetch_paginated(symbol, timeframe, gap_start, gap_end)
            with SessionLocal.begin() as db:
                symbol_row = db.scalar(select(Symbol).where(Symbol.symbol == symbol))
                for data in missing:
                    _, was_created = upsert_candle(db, symbol_row.id, timeframe, data)
                    backfilled += int(was_created)
        report = {"symbol": symbol, "timeframe": timeframe, "fetched": len(candles), "created": created, "updated": updated, "gaps_detected": len(gaps), "backfilled": backfilled}
        if config.analyze_historical_candles:
            report["analysis_backfill"] = await AnalysisBackfillService(session_factory=SessionLocal).run(
                symbol, timeframe, start_time=start_time, end_time=end_time, limit=None
            )
        log_event("INFO", "historical_sync", "sync_complete", "Historical synchronization completed", report)
        return report

    def detect_gaps(self, symbol: str, timeframe: str, start_time: datetime | None = None, end_time: datetime | None = None) -> list[tuple[datetime, datetime]]:
        step = timedelta(milliseconds=TIMEFRAME_MS[timeframe])
        with SessionLocal() as db:
            symbol_row = db.scalar(select(Symbol).where(Symbol.symbol == symbol))
            if not symbol_row:
                return []
            query = select(Candle.open_time).where(Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe)
            if start_time:
                query = query.where(Candle.open_time >= start_time)
            if end_time:
                query = query.where(Candle.open_time <= end_time)
            times = list(db.scalars(query.order_by(Candle.open_time)))
        gaps = []
        for previous, current in zip(times, times[1:]):
            if current - previous > step:
                gaps.append((previous + step, current - step))
        return gaps

    async def sync_configured(self) -> list[dict[str, Any]]:
        config = get_settings()
        reports = []
        for symbol in config.default_symbols:
            for timeframe in config.default_timeframes:
                try:
                    reports.append(await self.sync(symbol, timeframe))
                except Exception as exc:
                    log_event("ERROR", "historical_sync", "sync_failed", str(exc), {"symbol": symbol, "timeframe": timeframe})
        return reports
