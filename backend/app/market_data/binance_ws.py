import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import websockets

from app.core.config import get_settings
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData
from app.services.broadcast import broadcaster
from app.services.pipeline import process_closed_candle


class BinanceWebSocketManager:
    def __init__(self):
        self.settings = get_settings()
        self.running = False
        self.connected = False
        self.last_message_at: datetime | None = None
        self.reconnect_count = 0

    @property
    def streams(self) -> list[str]:
        return [f"{symbol.lower()}@kline_{timeframe}" for symbol in self.settings.default_symbols for timeframe in self.settings.default_timeframes]

    def status(self) -> dict:
        return {"running": self.running, "connected": self.connected, "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None, "reconnect_count": self.reconnect_count, "streams": self.streams}

    @staticmethod
    def normalize(message: dict) -> tuple[str, str, CandleData]:
        event = message.get("data", message)
        kline = event["k"]
        close_time = datetime.fromtimestamp(int(kline["T"]) / 1000, tz=timezone.utc)
        candle = CandleData(open_time=datetime.fromtimestamp(int(kline["t"]) / 1000, tz=timezone.utc), close_time=close_time, open=Decimal(kline["o"]), high=Decimal(kline["h"]), low=Decimal(kline["l"]), close=Decimal(kline["c"]), volume=Decimal(kline["v"]), quote_volume=Decimal(kline["q"]), trade_count=int(kline["n"]), taker_buy_base_volume=Decimal(kline["V"]), taker_buy_quote_volume=Decimal(kline["Q"]), is_closed=bool(kline["x"]))
        return event["s"], kline["i"], candle

    async def handle_message(self, raw: str) -> None:
        message = json.loads(raw)
        if "k" not in message.get("data", message):
            return
        symbol, timeframe, data = self.normalize(message)
        with SessionLocal.begin() as db:
            symbol_row = ensure_symbol(db, symbol)
            candle, _ = upsert_candle(db, symbol_row.id, timeframe, data)
            candle_id = candle.id
            payload = {"id": candle.id, "symbol": symbol, "timeframe": timeframe, "open_time": data.open_time.isoformat(), "close_time": data.close_time.isoformat(), "open": str(data.open), "high": str(data.high), "low": str(data.low), "close": str(data.close), "volume": str(data.volume), "is_closed": data.is_closed}
        await broadcaster.broadcast("candle_closed" if data.is_closed else "candle_update", payload)
        if data.is_closed:
            await process_closed_candle(candle_id)

    async def run(self) -> None:
        self.running = True
        attempt = 0
        query = "/".join(self.streams)
        url = f"{self.settings.binance_ws_base_url}?streams={query}"
        while self.running:
            try:
                log_event("INFO", "binance_ws", "connecting", "Connecting to Binance market streams", {"stream_count": len(self.streams)})
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10, max_queue=2048) as socket:
                    self.connected = True
                    attempt = 0
                    log_event("INFO", "binance_ws", "connected", "Binance market stream connected")
                    while self.running:
                        raw = await asyncio.wait_for(socket.recv(), timeout=45)
                        self.last_message_at = datetime.now(timezone.utc)
                        await self.handle_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                self.reconnect_count += 1
                delay = min(60, 2 ** attempt)
                attempt += 1
                log_event("WARNING", "binance_ws", "reconnecting", str(exc), {"delay_seconds": delay})
                if self.running:
                    await asyncio.sleep(delay)
        self.connected = False
        self.running = False

    async def stop(self) -> None:
        self.running = False


market_stream = BinanceWebSocketManager()

