import asyncio
import hashlib
import hmac
import json
import random
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
import websockets
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.core.config import get_settings
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.execution.credentials import load_stored_settings
from app.models import (
    ExecutionEvent,
    ExecutionFill,
    ExecutionOrder,
    LivePosition,
    ProtectiveOrder,
    Symbol,
    TradeSetup,
)
from app.services.broadcast import broadcaster

STATUS_MAP = {
    "NEW": "acknowledged",
    "PARTIALLY_FILLED": "partially_filled",
    "FILLED": "filled",
    "CANCELED": "canceled",
    "REJECTED": "exchange_rejected",
    "EXPIRED": "expired",
    "EXPIRED_IN_MATCH": "expired",
}


def utc_ms(value):
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc) if value else None


def normalize_event(message):
    raw = message.get("event", message)
    kind = raw.get("e")
    if kind == "executionReport":
        return {
            "type": "order",
            "event_time": utc_ms(raw.get("E")),
            "transaction_time": utc_ms(raw.get("T")),
            "symbol": raw.get("s"),
            "client_order_id": raw.get("c"),
            "exchange_order_id": str(raw.get("i"))
            if raw.get("i") is not None
            else None,
            "side": raw.get("S"),
            "order_type": raw.get("o"),
            "execution_type": raw.get("x"),
            "status": raw.get("X"),
            "cumulative_quantity": Decimal(raw.get("z", "0")),
            "cumulative_quote": Decimal(raw.get("Z", "0")),
            "last_quantity": Decimal(raw.get("l", "0")),
            "last_price": Decimal(raw.get("L", "0")),
            "commission": Decimal(raw.get("n", "0") or "0"),
            "commission_asset": raw.get("N"),
            "trade_id": str(raw.get("t")) if raw.get("t") not in (None, -1) else None,
            "raw": raw,
        }
    if kind == "outboundAccountPosition":
        return {
            "type": "balance",
            "event_time": utc_ms(raw.get("E")),
            "balances": [
                {"asset": x["a"], "free": Decimal(x["f"]), "locked": Decimal(x["l"])}
                for x in raw.get("B", [])
            ],
            "raw": raw,
        }
    if kind == "balanceUpdate":
        return {
            "type": "balance_delta",
            "event_time": utc_ms(raw.get("E")),
            "asset": raw.get("a"),
            "delta": Decimal(raw.get("d", "0")),
            "raw": raw,
        }
    if kind == "listStatus":
        return {
            "type": "order_list",
            "event_time": utc_ms(raw.get("E")),
            "symbol": raw.get("s"),
            "order_list_id": str(raw.get("g")) if raw.get("g") is not None else None,
            "list_status_type": raw.get("l"),
            "list_order_status": raw.get("L"),
            "raw": raw,
        }
    if kind in {"eventStreamTerminated", "serverShutdown"}:
        return {"type": "terminated", "event_time": utc_ms(raw.get("E")), "raw": raw}
    return {"type": "ignored", "event_name": kind, "raw": raw}


class BinanceUserStreamService:
    def __init__(
        self,
        session_factory=SessionLocal,
        connector=websockets.connect,
        settings_provider=get_settings,
        sleeper=asyncio.sleep,
    ):
        self.session_factory = session_factory
        self.connector = connector
        self.settings_provider = settings_provider
        self.sleeper = sleeper
        self._task = None
        self._lock = asyncio.Lock()
        self._socket = None
        self._balances = {}
        self.running = False
        self.connected = False
        self.last_message_at = None
        self.connected_at = None
        self.reconnect_count = 0
        self.last_error = None

    def status(self):
        uptime = (
            (datetime.now(timezone.utc) - self.connected_at).total_seconds()
            if self.connected and self.connected_at
            else 0
        )
        return {
            "running": self.running,
            "connected": self.connected,
            "last_message_at": self.last_message_at,
            "connected_at": self.connected_at,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
            "connection_uptime_seconds": round(uptime, 1),
        }

    def balances(self):
        return [{"asset": a, **v} for a, v in sorted(self._balances.items())]

    async def start(self):
        async with self._lock:
            if self._task and not self._task.done():
                return False
            self.running = True
            self.last_error = None
            self._task = asyncio.create_task(self._run(), name="binance-user-stream")
            return True

    async def stop(self):
        async with self._lock:
            self.running = False
            self.connected = False
            task = self._task
            self._task = None
            if self._socket:
                try:
                    await self._socket.close()
                except Exception:
                    pass
                self._socket = None
            if task and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        log_event(
            "INFO",
            "execution",
            "user_stream_disconnected",
            "Binance user stream stopped",
            {},
        )

    async def restart(self):
        await self.stop()
        return await self.start()

    def _credentials(self):
        settings = self.settings_provider()
        with self.session_factory() as db:
            _, configured = load_stored_settings(db, settings)
        if configured.binance_environment != "testnet":
            raise RuntimeError("User stream is restricted to Binance Spot Testnet")
        return configured

    def _subscription(self, settings):
        params = {
            "apiKey": settings.binance_api_key,
            "recvWindow": settings.binance_recv_window_ms,
            "timestamp": int(time.time() * 1000),
        }
        payload = urlencode(sorted(params.items()))
        params["signature"] = hmac.new(
            settings.binance_api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "id": str(uuid.uuid4()),
            "method": "userDataStream.subscribe.signature",
            "params": params,
        }

    async def _run(self):
        attempt = 0
        log_event(
            "INFO",
            "execution",
            "user_stream_starting",
            "Starting Binance Spot Testnet user stream",
            {},
        )
        while self.running:
            try:
                settings = self._credentials()
                request = self._subscription(settings)
                url = "wss://ws-api.testnet.binance.vision/ws-api/v3?returnRateLimits=false"
                async with self.connector(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_queue=2048,
                ) as socket:
                    self._socket = socket
                    await socket.send(json.dumps(request))
                    response = json.loads(
                        await asyncio.wait_for(socket.recv(), timeout=10)
                    )
                    if (
                        response.get("id") != request["id"]
                        or response.get("status") != 200
                    ):
                        raise RuntimeError(
                            f"Binance user stream subscription rejected ({response.get('status', 'unknown')})"
                        )
                    self.connected = True
                    self.connected_at = datetime.now(timezone.utc)
                    self.last_error = None
                    attempt = 0
                    log_event(
                        "INFO",
                        "execution",
                        "user_stream_connected",
                        "Binance Spot Testnet user stream connected",
                        {},
                    )
                    async for payload in socket:
                        event = normalize_event(json.loads(payload))
                        self.last_message_at = datetime.now(timezone.utc)
                        if event["type"] == "terminated":
                            raise ConnectionError("Binance terminated the user stream")
                        await self.process_event(event)
                    raise ConnectionError("Binance user stream connection closed")
            except HTTPException as exc:
                self.last_error = "credentials_not_available"
                self.running = False
                log_event(
                    "WARNING",
                    "execution",
                    "user_stream_error",
                    "Binance user stream credentials are unavailable",
                    {"status": exc.status_code},
                )
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                self._socket = None
                self.last_error = str(exc)[:300]
                self.reconnect_count += 1
                attempt += 1
                log_event(
                    "WARNING",
                    "execution",
                    "user_stream_reconnecting",
                    "Binance user stream reconnecting",
                    {"attempt": attempt, "error": self.last_error},
                )
                if self.running:
                    await self.sleeper(min(30, 2 ** min(attempt, 5)) + random.random())
        self.connected = False
        self._socket = None

    async def process_event(self, event):
        if event["type"] in {"balance", "balance_delta"}:
            await self._process_balance(event)
            return
        if event["type"] == "order":
            await self._process_order(event)
            return
        if event["type"] == "order_list":
            await self._process_order_list(event)

    async def _process_order_list(self, event):
        with self.session_factory.begin() as db:
            protection = db.scalar(select(ProtectiveOrder).where(
                ProtectiveOrder.order_list_id == event["order_list_id"]))
            if not protection:
                return
            position = db.get(LivePosition, protection.live_position_id)
            protection.raw_status_json = event["raw"]
            if event["list_order_status"] == "ALL_DONE":
                protection.status = "closed" if position.status == "closed" else "protection_failed"
                protection.closed_at = event["event_time"] or datetime.now(timezone.utc)
                if position.status != "closed":
                    position.protection_status = "unprotected"

    async def _process_balance(self, event):
        if event["type"] == "balance":
            for row in event["balances"]:
                self._balances[row["asset"]] = {
                    "available": str(row["free"]),
                    "locked": str(row["locked"]),
                    "updated_at": event["event_time"],
                }
        else:
            current = self._balances.setdefault(
                event["asset"], {"available": "0", "locked": "0", "updated_at": None}
            )
            current["available"] = str(Decimal(current["available"]) + event["delta"])
            current["updated_at"] = event["event_time"]
        safe = {
            "assets": [x["asset"] for x in event.get("balances", [])]
            or [event.get("asset")],
            "event_time": event["event_time"],
        }
        log_event(
            "INFO",
            "execution",
            "user_stream_balance_update",
            "Binance balance update received",
            safe,
        )
        await broadcaster.broadcast("execution_balance_update", safe)

    async def _process_order(self, event):
        with self.session_factory() as db:
            order = db.scalar(
                select(ExecutionOrder).where(
                    ExecutionOrder.client_order_id == event["client_order_id"]
                )
            )
            if not order:
                protection = db.scalar(
                    select(ProtectiveOrder).where(
                        (ProtectiveOrder.stop_client_order_id == event["client_order_id"])
                        | (ProtectiveOrder.take_profit_client_order_id == event["client_order_id"])
                    )
                )
                if protection:
                    self._process_protective_order(db, protection, event)
                    db.commit()
                    return
                db.add(
                    ExecutionEvent(
                        severity="INFO",
                        event_type="user_stream_external_order_ignored",
                        exchange="binance",
                        environment="testnet",
                        message="Ignored order update not managed by WaveScope",
                        metadata_json={
                            "symbol": event["symbol"],
                            "status": event["status"],
                        },
                    )
                )
                db.commit()
                return
            order.exchange_order_id = (
                event["exchange_order_id"] or order.exchange_order_id
            )
            order.executed_quantity = event["cumulative_quantity"]
            order.quote_quantity = event["cumulative_quote"]
            order.status = event["status"]
            order.execution_state = STATUS_MAP.get(
                event["status"], event["status"].lower()
            )
            order.raw_status_json = event["raw"]
            if event["cumulative_quantity"] > 0:
                order.average_fill_price = (
                    event["cumulative_quote"] / event["cumulative_quantity"]
                )
            now = (
                event["transaction_time"]
                or event["event_time"]
                or datetime.now(timezone.utc)
            )
            if event["status"] == "FILLED":
                order.filled_at = now
            elif event["status"] == "CANCELED":
                order.canceled_at = now
            fill_added = False
            if event["trade_id"] and event["last_quantity"] > 0:
                fill = ExecutionFill(
                    execution_order_id=order.id,
                    exchange_trade_id=event["trade_id"],
                    price=event["last_price"],
                    quantity=event["last_quantity"],
                    quote_quantity=event["last_price"] * event["last_quantity"],
                    commission=event["commission"],
                    commission_asset=event["commission_asset"] or "",
                    filled_at=now,
                )
                db.add(fill)
                try:
                    db.flush()
                    fill_added = True
                except IntegrityError:
                    db.rollback()
                    return
            position_id = None
            if fill_added:
                position_id = self._update_position(db, order, event, now)
            db.add(
                ExecutionEvent(
                    severity="INFO",
                    event_type="execution_filled" if event["status"] == "FILLED" else "user_stream_order_update",
                    exchange="binance",
                    environment=order.environment,
                    symbol_id=order.symbol_id,
                    trade_setup_id=order.trade_setup_id,
                    execution_order_id=order.id,
                    message=f"Managed order updated to {event['status']}",
                    metadata_json={
                        "status": event["status"],
                        "execution_type": event["execution_type"],
                    },
                )
            )
            db.commit()
            payload = {
                "order_id": order.id,
                "client_order_id": order.client_order_id,
                "status": order.status,
                "execution_state": order.execution_state,
                "executed_quantity": str(order.executed_quantity),
                "average_fill_price": str(order.average_fill_price)
                if order.average_fill_price is not None
                else None,
            }
        log_event(
            "INFO",
            "execution",
            "execution_filled" if event["status"] == "FILLED" else "user_stream_fill" if fill_added else "user_stream_order_update",
            "Binance managed order update received",
            payload,
        )
        await broadcaster.broadcast("execution_order_update", payload)
        if position_id and event["side"] == "BUY" and event["status"] == "FILLED":
            from app.execution.protection import SpotProtectionService

            await SpotProtectionService(
                session_factory=self.session_factory
            ).establish(position_id)

    def _update_position(self, db, order, event, now):
        symbol = db.get(Symbol, order.symbol_id)
        position = db.scalar(
            select(LivePosition)
            .where(
                LivePosition.originating_trade_setup_id == order.trade_setup_id,
                LivePosition.environment == order.environment,
            )
            .order_by(LivePosition.id.desc())
        )
        quantity = event["last_quantity"]
        if event["side"] == "BUY":
            owned = quantity - (
                event["commission"]
                if event["commission_asset"] == symbol.base_asset
                else Decimal("0")
            )
            if not position:
                setup = db.get(TradeSetup, order.trade_setup_id)
                position = LivePosition(
                    environment=order.environment,
                    symbol_id=order.symbol_id,
                    originating_trade_setup_id=order.trade_setup_id,
                    direction="long",
                    status="open",
                    base_quantity=owned,
                    remaining_quantity=owned,
                    average_entry=event["last_price"],
                    stop_loss=Decimal("0"),
                    take_profit_1=None,
                    take_profit_2=None,
                    take_profit_3=None,
                    protection_status="protection_pending",
                    opened_at=now,
                )
                db.add(position)
            else:
                total = position.base_quantity + owned
                position.average_entry = (
                    (
                        (position.average_entry * position.base_quantity)
                        + (event["last_price"] * owned)
                    )
                    / total
                    if total
                    else position.average_entry
                )
                position.base_quantity = total
                position.remaining_quantity += owned
                position.status = "open"
            if event["commission_asset"] == symbol.quote_asset:
                position.total_fees += event["commission"]
        elif position:
            sold = min(quantity, position.remaining_quantity)
            quote_fee = (
                event["commission"]
                if event["commission_asset"] == symbol.quote_asset
                else Decimal("0")
            )
            position.realized_pnl += (
                event["last_price"] - position.average_entry
            ) * sold - quote_fee
            position.total_fees += quote_fee
            position.remaining_quantity -= sold
            if position.remaining_quantity <= 0:
                position.remaining_quantity = Decimal("0")
                position.status = "closed"
                position.closed_at = now
        db.flush()
        return position.id if position else None

    def _process_protective_order(self, db, protection, event):
        position = db.get(LivePosition, protection.live_position_id)
        symbol = db.get(Symbol, protection.symbol_id)
        if event["client_order_id"] == protection.stop_client_order_id:
            protection.stop_exchange_order_id = event["exchange_order_id"]
            leg = "stop_loss"
        else:
            protection.take_profit_exchange_order_id = event["exchange_order_id"]
            leg = "take_profit_1"
        protection.raw_status_json = event["raw"]
        now = event["transaction_time"] or event["event_time"] or datetime.now(timezone.utc)
        if event["last_quantity"] > 0 and event["side"] == "SELL":
            sold = min(event["last_quantity"], position.remaining_quantity)
            quote_fee = event["commission"] if event["commission_asset"] == symbol.quote_asset else Decimal("0")
            position.realized_pnl += (event["last_price"] - position.average_entry) * sold - quote_fee
            position.total_fees += quote_fee
            position.remaining_quantity -= sold
        if event["status"] == "FILLED":
            protection.status = "closed"
            protection.closed_at = now
            if position.remaining_quantity <= 0:
                position.remaining_quantity = Decimal("0")
                position.status = "closed"
                position.protection_status = "closed"
                position.closed_at = now
                position.exit_reason = "take_profit" if leg == "take_profit_1" else "stop_loss"
            else:
                position.status = "partially_closed"
                position.protection_status = "partially_protected"
            event_type, severity = "protection_exit_filled", "INFO"
        elif event["status"] in {"REJECTED", "EXPIRED"}:
            protection.status = "protection_failed"
            protection.rejection_reason = event["status"].lower()
            position.protection_status = "unprotected"
            event_type, severity = "protection_failed", "CRITICAL"
        elif event["status"] == "CANCELED" and position.status != "closed":
            protection.status = "protection_failed"
            protection.rejection_reason = "protective_leg_canceled"
            position.protection_status = "unprotected"
            event_type, severity = "protection_failed", "CRITICAL"
        else:
            event_type, severity = "protection_order_update", "INFO"
        db.add(
            ExecutionEvent(
                severity=severity,
                event_type=event_type,
                exchange="binance",
                environment=protection.environment,
                symbol_id=symbol.id,
                trade_setup_id=position.originating_trade_setup_id,
                live_position_id=position.id,
                message=f"Protective {leg} updated to {event['status']}",
                metadata_json={
                    "leg": leg,
                    "status": event["status"],
                    "client_order_id": event["client_order_id"],
                    "remaining_quantity": str(position.remaining_quantity),
                },
            )
        )


user_stream = BinanceUserStreamService()
