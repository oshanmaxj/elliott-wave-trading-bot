import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.core.config import get_settings
from app.schemas.common import CandleData


class BinanceAPIError(RuntimeError):
    pass


def milliseconds(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


class BinanceRESTClient:
    def __init__(self, base_url: str | None = None, max_retries: int = 5):
        self.base_url = (base_url or get_settings().binance_rest_base_url).rstrip("/")
        self.max_retries = max_retries

    @staticmethod
    def normalize_kline(row: list, now: datetime | None = None) -> CandleData:
        now = now or datetime.now(timezone.utc)
        close_time = from_ms(int(row[6]))
        return CandleData(
            open_time=from_ms(int(row[0])), close_time=close_time,
            open=Decimal(row[1]), high=Decimal(row[2]), low=Decimal(row[3]), close=Decimal(row[4]),
            volume=Decimal(row[5]), quote_volume=Decimal(row[7]), trade_count=int(row[8]),
            taker_buy_base_volume=Decimal(row[9]), taker_buy_quote_volume=Decimal(row[10]),
            is_closed=close_time < now,
        )

    async def _request(self, params: dict) -> list[list]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.get("/fapi/v1/klines", params=params)
                    if response.status_code in {418, 429} or response.status_code >= 500:
                        delay = min(30, 2 ** attempt)
                        if "Retry-After" in response.headers:
                            delay = max(delay, int(response.headers["Retry-After"]))
                        await asyncio.sleep(delay)
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        raise BinanceAPIError(f"unexpected Binance response: {payload}")
                    return payload
                except (httpx.HTTPError, ValueError) as exc:
                    if attempt == self.max_retries - 1:
                        raise BinanceAPIError(str(exc)) from exc
                    await asyncio.sleep(min(30, 2 ** attempt))
        raise BinanceAPIError("Binance request failed after retries")

    async def fetch_historical_klines(self, symbol: str, timeframe: str, start_time: datetime | None = None, end_time: datetime | None = None, limit: int = 500) -> list[CandleData]:
        params = {"symbol": symbol, "interval": timeframe, "limit": min(limit, 1500)}
        if start_time:
            params["startTime"] = milliseconds(start_time)
        if end_time:
            params["endTime"] = milliseconds(end_time)
        return [self.normalize_kline(row) for row in await self._request(params)]

    async def fetch_paginated(self, symbol: str, timeframe: str, start_time: datetime | None, end_time: datetime | None, page_limit: int = 1500) -> list[CandleData]:
        results: list[CandleData] = []
        cursor = start_time
        while True:
            page = await self.fetch_historical_klines(symbol, timeframe, cursor, end_time, page_limit)
            if not page:
                break
            results.extend(page)
            next_ms = int(page[-1].open_time.timestamp() * 1000) + 1
            cursor = datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc)
            if len(page) < page_limit or (end_time and cursor > end_time):
                break
        by_time = {item.open_time: item for item in results}
        return [by_time[key] for key in sorted(by_time)]

