import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import websockets

from app.core.config import get_settings
from app.core.constants import BINANCE_INTERVALS, TIMEFRAMES
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
        self.connected_at: datetime | None = None
        self._socket = None
        self.current_candles: dict[str, dict] = {}

    @property
    def streams(self) -> list[str]:
        # Subscribe to every centrally supported interval. Runtime bot settings are
        # database-backed and can change after this long-lived socket connects;
        # filtering at candle dispatch keeps those settings authoritative without
        # silently missing newly enabled intervals until a process restart.
        streams = (
            f"{symbol.lower()}@kline_{BINANCE_INTERVALS[timeframe]}"
            for symbol in self.settings.default_symbols
            for timeframe in TIMEFRAMES
        )
        return list(dict.fromkeys(streams))

    def status(self) -> dict:
        uptime = (datetime.now(timezone.utc) - self.connected_at).total_seconds() if self.connected_at else None
        return {"running": self.running, "connected": self.connected, "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None, "reconnect_count": self.reconnect_count, "connection_uptime_seconds": uptime, "streams": self.streams}

    def market_state(self, symbol: str, timeframe: str) -> dict:
        key = f"{symbol.upper()}:{timeframe}"
        age = (datetime.now(timezone.utc) - self.last_message_at).total_seconds() if self.last_message_at else None
        return {"symbol": symbol.upper(), "timeframe": timeframe, "connected": self.connected, "last_event_at": self.last_message_at, "event_age_seconds": age, "current_candle": self.current_candles.get(key)}

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
        self.current_candles[f"{symbol}:{timeframe}"] = payload
        await broadcaster.broadcast("candle_closed" if data.is_closed else "candle_update", payload)
        if data.is_closed:
            log_event("INFO", "binance_ws", "live_candle_closed", "Live candle closed and persisted", {"symbol": symbol, "timeframe": timeframe, "candle_id": candle_id})
            try:
                await process_closed_candle(candle_id)
            except Exception as exc:
                # A strategy failure must not tear down an otherwise healthy market
                # socket and masquerade as a connectivity incident.
                log_event("ERROR", "analysis", "closed_candle_failed", str(exc) or type(exc).__name__, {"symbol": symbol, "timeframe": timeframe, "candle_id": candle_id, "exception_type": type(exc).__name__})

    async def run(self) -> None:
        if self.running:
            log_event("WARNING", "binance_ws", "duplicate_manager_ignored", "Market stream manager is already running")
            return
        self.running = True
        attempt = 0
        query = "/".join(self.streams)
        url = f"{self.settings.binance_ws_base_url}?streams={query}"
        while self.running:
            try:
                log_event("INFO", "binance_ws", "connecting", "Connecting to Binance market streams", {"stream_count": len(self.streams)})
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10, max_queue=2048) as socket:
                    self._socket = socket
                    self.connected = True
                    self.connected_at = datetime.now(timezone.utc)
                    attempt = 0
                    log_event("INFO", "binance_ws", "market_stream_connected", "Binance market stream connected")
                    while self.running:
                        raw = await socket.recv()
                        self.last_message_at = datetime.now(timezone.utc)
                        try:
                            await self.handle_message(raw)
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                            log_event("WARNING", "binance_ws", "malformed_market_event", str(exc) or type(exc).__name__, {"exception_type": type(exc).__name__})
            except asyncio.CancelledError:
                break
            except Exception as exc:
                uptime = (datetime.now(timezone.utc) - self.connected_at).total_seconds() if self.connected_at else 0
                self.connected = False
                self.reconnect_count += 1
                delay = min(60, 2 ** attempt)
                attempt += 1
                context = {"exception_type": type(exc).__name__, "exception_message": str(exc), "close_code": getattr(exc, "code", None), "close_reason": getattr(exc, "reason", None), "reconnect_attempt": attempt, "connection_uptime_seconds": round(uptime, 3), "delay_seconds": delay}
                log_event("WARNING", "binance_ws", "market_stream_disconnected", str(exc) or type(exc).__name__, context)
                if self.running:
                    await asyncio.sleep(delay)
        self.connected = False
        self.connected_at = None
        self._socket = None
        self.running = False

    async def stop(self) -> None:
        self.running = False
        if self._socket is not None:
            await self._socket.close(code=1000, reason="service shutdown")


market_stream = BinanceWebSocketManager()
