"""Resumable retention-based Binance public OHLCV backfill."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.constants import TIMEFRAME_MS
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.market_data.binance_rest import BinanceRESTClient
from app.models import AnalysisSnapshot, Candle, Symbol
from app.repositories.market import ensure_symbol, upsert_candle


def utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))


def aligned_range(start: datetime, end: datetime, timeframe: str) -> tuple[datetime, datetime]:
    """Return the candle-open boundaries needed to cover [start, end)."""
    step_ms = TIMEFRAME_MS[timeframe]
    start_ms, end_ms = int(utc(start).timestamp() * 1000), int(utc(end).timestamp() * 1000)
    first = start_ms // step_ms * step_ms
    last = max(first, (end_ms - 1) // step_ms * step_ms)
    return from_timestamp(first), from_timestamp(last)


def from_timestamp(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def latest_closed_time(timeframe: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    step_ms = TIMEFRAME_MS[timeframe]
    current_open_ms = int(now.timestamp() * 1000) // step_ms * step_ms
    return datetime.fromtimestamp((current_open_ms - 1) / 1000, tz=timezone.utc)


class HistoricalBackfillService:
    def __init__(self, client=None, session_factory=None):
        self.client = client or BinanceRESTClient()
        self.session_factory = session_factory or SessionLocal
        self._statuses: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @staticmethod
    def key(symbol: str, timeframe: str) -> str:
        return f"{symbol.upper()}:{timeframe}"

    def retention_days(self, timeframe: str) -> int:
        config = get_settings()
        return {
            "1m": config.history_days_1m,
            "5m": config.history_days_5m,
            "15m": config.history_days_15m,
            "1h": config.history_days_1h,
            "4h": config.history_days_4h,
        }[timeframe]

    def status(self, symbol=None, timeframe=None):
        if symbol and timeframe:
            return self._statuses.get(self.key(symbol, timeframe), {
                "symbol": symbol, "timeframe": timeframe, "status": "idle",
                "processed_batches": 0, "inserted_candles": 0,
                "updated_candles": 0, "remaining_estimate": 0, "last_error": None,
            })
        return list(self._statuses.values())

    def coverage(self) -> dict:
        result: dict[str, dict] = {}
        now = datetime.now(timezone.utc)
        config = get_settings()
        with self.session_factory() as db:
            for symbol_name in config.default_symbols:
                symbol = db.scalar(select(Symbol).where(Symbol.symbol == symbol_name))
                result[symbol_name] = {}
                for timeframe in config.default_timeframes:
                    required_from = now - timedelta(days=self.retention_days(timeframe))
                    row = (None, None, 0) if not symbol else db.execute(select(
                        func.min(Candle.open_time), func.max(Candle.close_time),
                        func.count(Candle.id),
                    ).where(
                        Candle.symbol_id == symbol.id, Candle.timeframe == timeframe,
                        Candle.is_closed.is_(True),
                    )).one()
                    available_from, available_to, count = row
                    required_compare = required_from
                    required_to_compare = latest_closed_time(timeframe, now)
                    if available_from and available_from.tzinfo is None:
                        required_compare = required_compare.replace(tzinfo=None)
                        required_to_compare = required_to_compare.replace(tzinfo=None)
                    complete = bool(
                        available_from and available_to
                        and available_from <= required_compare
                        and available_to >= required_to_compare
                    )
                    result[symbol_name][timeframe] = {
                        "available_from": available_from.isoformat() if available_from else None,
                        "available_to": available_to.isoformat() if available_to else None,
                        "candle_count": count, "required_from": required_from.isoformat(),
                        "required_to": latest_closed_time(timeframe, now).isoformat(),
                        "coverage_complete": complete,
                    }
        return result

    def schedule(self, symbol: str, timeframe: str, days=None, start=None, end=None):
        key = self.key(symbol, timeframe)
        current = self._tasks.get(key)
        if current and not current.done():
            return self.status(symbol, timeframe)
        task = asyncio.create_task(
            self.run(symbol, timeframe, days=days, start=start, end=end),
            name=f"history-backfill-{key}",
        )
        self._tasks[key] = task
        return {"symbol": symbol, "timeframe": timeframe, "status": "scheduled"}

    async def run(self, symbol: str, timeframe: str, days=None, start=None, end=None):
        config = get_settings()
        end = end or latest_closed_time(timeframe)
        start = start or datetime.now(timezone.utc) - timedelta(
            days=days or self.retention_days(timeframe)
        )
        if start >= end:
            raise ValueError("historical range start must be before end")
        requested_start, requested_end = utc(start), utc(end)
        start, end = aligned_range(requested_start, requested_end, timeframe)
        key = self.key(symbol, timeframe)
        step = timedelta(milliseconds=TIMEFRAME_MS[timeframe])
        with self.session_factory.begin() as db:
            symbol_row = ensure_symbol(db, symbol)
            earliest, latest = db.execute(select(
                func.min(Candle.open_time), func.max(Candle.open_time)
            ).where(
                Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe,
                Candle.is_closed.is_(True),
            )).one()
            times = list(db.scalars(select(Candle.open_time).where(
                Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe,
                Candle.is_closed.is_(True), Candle.open_time >= start,
                Candle.open_time <= end,
            ).order_by(Candle.open_time)))
        if earliest is not None and earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=timezone.utc)
        if latest is not None and latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        times = [item.replace(tzinfo=timezone.utc) if item.tzinfo is None else item.astimezone(timezone.utc) for item in times]
        ranges = []
        if earliest is None:
            ranges.append((start, end))
        else:
            if earliest > start:
                ranges.append((start, earliest - step))
            if latest < end:
                ranges.append((latest + step, end))
            ranges.extend(
                (previous + step, current - step)
                for previous, current in zip(times, times[1:])
                if current - previous > step
            )
        ranges.sort(key=lambda item: item[0])
        estimate = sum(max(0, int((b - a) / step) + 1) for a, b in ranges)
        state = self._statuses[key] = {
            "symbol": symbol, "timeframe": timeframe, "status": "running",
            "requested_from": requested_start.isoformat(), "requested_to": requested_end.isoformat(),
            "aligned_from": start.isoformat(), "aligned_to": end.isoformat(),
            "processed_batches": 0, "inserted_candles": 0,
            "updated_candles": 0, "remaining_estimate": estimate, "last_error": None,
        }
        log_event("INFO", "historical_backfill", "history_backfill_started", "Historical backfill started", state)
        try:
            for range_start, range_end in ranges:  # oldest missing range first
                cursor = range_start
                while cursor <= range_end:
                    page = await self.client.fetch_historical_klines(
                        symbol, timeframe, cursor, range_end, 1500
                    )
                    closed = [item for item in page if item.is_closed]
                    request_context = {
                        "symbol": symbol, "timeframe": timeframe,
                        "cursor": cursor.isoformat(), "batch_end": range_end.isoformat(),
                        "limit": 1500,
                    }
                    if not page:
                        log_event("WARNING", "historical_backfill", "history_backfill_empty_batch", "Binance returned no candles for a missing range", request_context)
                        break
                    with self.session_factory.begin() as db:
                        symbol_row = ensure_symbol(db, symbol)
                        for data in closed:
                            _, created = upsert_candle(db, symbol_row.id, timeframe, data)
                            state["inserted_candles"] += int(created)
                            state["updated_candles"] += int(not created)
                    state["processed_batches"] += 1
                    state["remaining_estimate"] = max(
                        0, state["remaining_estimate"] - len(closed)
                    )
                    next_cursor = page[-1].open_time + step
                    log_event("INFO", "historical_backfill", "history_backfill_batch", "Historical batch stored", {
                        **request_context, "returned_candles": len(page), "closed_candles": len(closed),
                        "inserted_count": state["inserted_candles"], "updated_count": state["updated_candles"],
                        "next_cursor": next_cursor.isoformat(), "remaining_range": max(0, int((range_end - next_cursor) / step) + 1),
                    })
                    if next_cursor <= cursor:
                        raise RuntimeError(f"Binance pagination cursor did not advance from {cursor.isoformat()}")
                    if next_cursor > range_end:
                        break
                    cursor = next_cursor
                    await asyncio.sleep(config.history_backfill_rate_delay)
            verification = self.verify(symbol, timeframe, start, end)
            state["coverage"] = verification
            if not verification["coverage_complete"]:
                raise RuntimeError(f"historical coverage remains incomplete: {verification['missing_ranges'][:5]}")
            state["status"], state["remaining_estimate"] = "completed", 0
            log_event("INFO", "historical_backfill", "history_backfill_completed", "Historical backfill completed", state)
        except Exception as exc:
            state["status"], state["last_error"] = "failed", str(exc)
            log_event("ERROR", "historical_backfill", "history_backfill_failed", str(exc), state)
            raise
        return state

    def verify(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> dict:
        step = timedelta(milliseconds=TIMEFRAME_MS[timeframe])
        with self.session_factory() as db:
            symbol_row = db.scalar(select(Symbol).where(Symbol.symbol == symbol.upper()))
            times = [] if not symbol_row else list(db.scalars(select(Candle.open_time).where(
                Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe,
                Candle.is_closed.is_(True), Candle.open_time >= start, Candle.open_time <= end,
            ).order_by(Candle.open_time)))
        times = [item.replace(tzinfo=timezone.utc) if item.tzinfo is None else item.astimezone(timezone.utc) for item in times]
        start, end = utc(start), utc(end)
        missing, cursor = [], start
        for current in times:
            if current.tzinfo is None and cursor.tzinfo is not None:
                current = current.replace(tzinfo=timezone.utc)
            if current > cursor:
                missing.append((cursor.isoformat(), (current - step).isoformat()))
            if current >= cursor:
                cursor = current + step
        if cursor <= end:
            missing.append((cursor.isoformat(), end.isoformat()))
        return {
            "available_from": times[0].isoformat() if times else None,
            "available_to": times[-1].isoformat() if times else None,
            "candle_count": len(times), "expected_count": int((end - start) / step) + 1,
            "missing_ranges": missing, "coverage_complete": not missing,
        }

    async def run_configured(self):
        config = get_settings()
        # Candle synchronization and derived analysis must advance together. The
        # incremental analysis service skips snapshots already processed at the
        # current SMC version and never deletes historical analysis records.
        from app.services.analysis_backfill import AnalysisBackfillService

        analysis = AnalysisBackfillService(session_factory=self.session_factory)
        for symbol in config.default_symbols:
            for timeframe in config.default_timeframes:
                try:
                    await self.run(symbol, timeframe)
                    if config.analyze_historical_candles:
                        with self.session_factory() as db:
                            latest_analysis = db.scalar(
                                select(func.max(AnalysisSnapshot.generated_at))
                                .join(Symbol, AnalysisSnapshot.symbol_id == Symbol.id)
                                .where(Symbol.symbol == symbol, AnalysisSnapshot.timeframe == timeframe)
                            )
                        await analysis.run(symbol, timeframe, start_time=latest_analysis)
                except Exception as exc:
                    log_event("ERROR", "historical_backfill", "configured_backfill_failed", str(exc), {"symbol": symbol, "timeframe": timeframe, "exception_type": type(exc).__name__})
                    continue


historical_backfill = HistoricalBackfillService()
