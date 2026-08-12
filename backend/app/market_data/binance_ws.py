import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import websockets
from sqlalchemy import select

from app.core.config import get_settings
from app.core.constants import BINANCE_INTERVALS, TIMEFRAMES
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.models import Candle
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData
from app.services.broadcast import broadcaster
from app.services.pipeline import process_closed_candle


class BinanceWebSocketManager:
    def __init__(self, session_factory=None, candle_processor=None):
        self.settings = get_settings()
        self.session_factory = session_factory or SessionLocal
        self.candle_processor = candle_processor or process_closed_candle
        self.running = False
        self.connected = False
        self.last_message_at: datetime | None = None
        self.reconnect_count = 0
        self.connected_at: datetime | None = None
        self._socket = None
        self.current_candles: dict[str, dict] = {}
        self.messages_received = 0
        self.kline_messages_received = 0
        self.last_kline_at: datetime | None = None
        self.last_closed_candle_at: datetime | None = None

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
        return {"running": self.running, "connected": self.connected, "messages_received": self.messages_received, "kline_messages_received": self.kline_messages_received, "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None, "last_kline_at": self.last_kline_at.isoformat() if self.last_kline_at else None, "last_closed_candle_at": self.last_closed_candle_at.isoformat() if self.last_closed_candle_at else None, "reconnect_count": self.reconnect_count, "connection_uptime_seconds": uptime, "streams": self.streams}

    def market_state(self, symbol: str, timeframe: str) -> dict:
        key = f"{symbol.upper()}:{timeframe}"
        age = (datetime.now(timezone.utc) - self.last_message_at).total_seconds() if self.last_message_at else None
        return {"symbol": symbol.upper(), "timeframe": timeframe, **self.status(), "last_event_at": self.last_message_at, "event_age_seconds": age, "current_candle": self.current_candles.get(key)}

    @property
    def combined_stream_url(self) -> str:
        parsed = urlsplit(self.settings.binance_ws_base_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/ws"):
            path = f"{path[:-3]}/stream"
        elif not path.endswith("/stream"):
            path = f"{path}/stream" if path else "/stream"
        base = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        return f"{base}?streams={'/'.join(self.streams)}"

    @staticmethod
    def normalize(message: dict) -> tuple[str, str, CandleData]:
        event = message.get("data", message)
        kline = event["k"]
        close_time = datetime.fromtimestamp(int(kline["T"]) / 1000, tz=timezone.utc)
        candle = CandleData(open_time=datetime.fromtimestamp(int(kline["t"]) / 1000, tz=timezone.utc), close_time=close_time, open=Decimal(kline["o"]), high=Decimal(kline["h"]), low=Decimal(kline["l"]), close=Decimal(kline["c"]), volume=Decimal(kline["v"]), quote_volume=Decimal(kline["q"]), trade_count=int(kline["n"]), taker_buy_base_volume=Decimal(kline["V"]), taker_buy_quote_volume=Decimal(kline["Q"]), is_closed=bool(kline["x"]))
        return event["s"], kline["i"], candle

    async def handle_message(self, raw: str) -> None:
        now = datetime.now(timezone.utc)
        self.messages_received += 1
        self.last_message_at = now
        if self.messages_received == 1 or self.messages_received % 1000 == 0:
            log_event("INFO", "binance_ws", "market_ws_message_received", "Market WebSocket message received", {"messages_received": self.messages_received})
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            log_event("WARNING", "binance_ws", "market_ws_parse_error", str(exc) or type(exc).__name__, {"exception_type": type(exc).__name__, "messages_received": self.messages_received})
            return
        event = message.get("data", message) if isinstance(message, dict) else None
        if not isinstance(event, dict) or event.get("e") != "kline" or not isinstance(event.get("k"), dict):
            if self.messages_received <= 5 or self.messages_received % 1000 == 0:
                log_event("DEBUG", "binance_ws", "market_ws_message_ignored", "Non-kline market message ignored", {"event_type": event.get("e") if isinstance(event, dict) else None})
            return
        try:
            symbol, timeframe, data = self.normalize(message)
        except (KeyError, TypeError, ValueError) as exc:
            log_event("WARNING", "binance_ws", "market_ws_parse_error", str(exc) or type(exc).__name__, {"exception_type": type(exc).__name__, "event_type": event.get("e")})
            return
        if timeframe not in TIMEFRAMES or symbol.upper() not in self.settings.default_symbols:
            log_event("DEBUG", "binance_ws", "market_ws_message_ignored", "Unsupported kline stream ignored", {"symbol": symbol, "timeframe": timeframe})
            return
        self.kline_messages_received += 1
        self.last_kline_at = now
        with self.session_factory.begin() as db:
            symbol_row = ensure_symbol(db, symbol)
            existing = db.scalar(select(Candle).where(Candle.symbol_id == symbol_row.id, Candle.timeframe == timeframe, Candle.open_time == data.open_time))
            already_closed = bool(existing and existing.is_closed)
            candle, _ = upsert_candle(db, symbol_row.id, timeframe, data)
            candle_id = candle.id
            payload = {"id": candle.id, "symbol": symbol, "timeframe": timeframe, "open_time": data.open_time.isoformat(), "close_time": data.close_time.isoformat(), "open": str(data.open), "high": str(data.high), "low": str(data.low), "close": str(data.close), "volume": str(data.volume), "is_closed": data.is_closed}
        self.current_candles[f"{symbol}:{timeframe}"] = payload
        await broadcaster.broadcast("candle_closed" if data.is_closed else "candle_update", payload)
        if data.is_closed and not already_closed:
            self.last_closed_candle_at = now
            log_event("INFO", "binance_ws", "live_candle_closed", "Live candle closed and persisted", {"symbol": symbol, "timeframe": timeframe, "candle_id": candle_id})
            try:
                await self.candle_processor(candle_id)
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
        url = self.combined_stream_url
        while self.running:
            try:
                log_event("INFO", "binance_ws", "connecting", "Connecting to Binance market streams", {"stream_count": len(self.streams), "url": url})
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=10, max_queue=2048) as socket:
                    self._socket = socket
                    self.connected = True
                    self.connected_at = datetime.now(timezone.utc)
                    attempt = 0
                    log_event("INFO", "binance_ws", "market_stream_connected", "Binance market stream connected")
                    while self.running:
                        raw = await socket.recv()
                        try:
                            await self.handle_message(raw)
                        except Exception as exc:
                            log_event("ERROR", "binance_ws", "market_ws_parse_error", str(exc) or type(exc).__name__, {"exception_type": type(exc).__name__})
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
