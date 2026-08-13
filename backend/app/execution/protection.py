from datetime import datetime, timezone
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.execution.binance import BinanceSpotClient
from app.execution.credentials import load_stored_settings
from app.execution.filters import (
    floor_quantity_to_step,
    price_tick,
    quantity_limits,
    serialize_price,
    serialize_quantity,
    validate_notional,
)
from app.models import (
    ExecutionEvent,
    LivePosition,
    ProtectiveOrder,
    Symbol,
    TradeSetup,
)


class SpotProtectionService:
    """Protect a filled Spot BUY with one full-quantity TP1/SL OCO list."""

    def __init__(
        self,
        session_factory=SessionLocal,
        client_factory=BinanceSpotClient,
        settings=None,
    ):
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.settings = settings or get_settings()

    def _event(self, db, event_type, position, symbol, *, severity="INFO", reason=None, protection=None, metadata=None):
        details = {
            "symbol": symbol.symbol,
            "position_id": position.id,
            "protection_status": position.protection_status,
            "reason": reason,
            **(metadata or {}),
        }
        db.add(
            ExecutionEvent(
                severity=severity,
                event_type=event_type,
                exchange="binance",
                environment="testnet",
                symbol_id=symbol.id,
                trade_setup_id=position.originating_trade_setup_id,
                live_position_id=position.id,
                message=reason or event_type.replace("_", " "),
                metadata_json=details,
            )
        )
        log_event(severity, "protection", event_type, reason or event_type.replace("_", " "), details)

    async def establish(self, position_id: int) -> dict:
        client = None
        with self.session_factory() as db:
            position = db.scalar(
                select(LivePosition)
                .where(LivePosition.id == position_id)
                .with_for_update()
            )
            if not position or position.status == "closed":
                return {"protected": False, "reason": "position_not_open"}
            existing = db.scalar(
                select(ProtectiveOrder).where(
                    ProtectiveOrder.live_position_id == position.id,
                    ProtectiveOrder.status.in_(["submitting", "protected"]),
                )
            )
            if existing:
                return {"protected": existing.status == "protected", "reason": "protection_already_exists", "protection_id": existing.id}
            setup = db.get(TradeSetup, position.originating_trade_setup_id)
            symbol = db.get(Symbol, position.symbol_id)
            reason = None
            if not setup or setup.stop_loss is None:
                reason = "missing_protective_stop"
            elif setup.take_profit_1 is None:
                reason = "missing_protective_target"
            elif not setup.stop_loss < position.average_entry < setup.take_profit_1:
                reason = "invalid_protective_geometry"
            elif position.remaining_quantity <= 0:
                reason = "invalid_protective_quantity"
            if reason:
                position.protection_status = "unprotected"
                self._event(db, "protection_failed", position, symbol, severity="CRITICAL", reason=reason)
                db.commit()
                return {"protected": False, "reason": reason}
            position.stop_loss = setup.stop_loss
            position.take_profit_1 = setup.take_profit_1
            position.take_profit_2 = setup.take_profit_2
            position.take_profit_3 = setup.take_profit_3
            position.protection_status = "protection_pending"
            self._event(db, "protection_pending", position, symbol)
            db.commit()

        try:
            with self.session_factory() as db:
                _, execution_settings = load_stored_settings(db, self.settings)
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
            client = self.client_factory(execution_settings)
            info = (await client.exchange_info(symbol.symbol))["symbols"][0]
            minimum, maximum, step = quantity_limits(info, market=False)
            tick = price_tick(info)
            if step <= 0:
                raise ValueError("invalid_protective_quantity_step")
            if tick <= 0:
                raise ValueError("invalid_protective_price_tick")
            quantity = floor_quantity_to_step(position.remaining_quantity, step)
            stop = floor_quantity_to_step(position.stop_loss, tick)
            target = floor_quantity_to_step(position.take_profit_1, tick)
            if quantity <= 0 or quantity < minimum or quantity > maximum:
                raise ValueError("invalid_protective_quantity")
            if not stop < position.average_entry < target:
                raise ValueError("invalid_protective_geometry")
            notional_reasons = list(
                dict.fromkeys(
                    validate_notional(stop, quantity, info)
                    + validate_notional(target, quantity, info)
                )
            )
            if notional_reasons:
                raise ValueError(notional_reasons[0])
            suffix = f"{position.id}"
            params = {
                "symbol": symbol.symbol,
                "side": "SELL",
                "quantity": serialize_quantity(quantity, step),
                "listClientOrderId": f"ws-test-{suffix}-protect",
                "aboveType": "LIMIT_MAKER",
                "abovePrice": serialize_price(target, tick),
                "aboveClientOrderId": f"ws-test-{suffix}-tp1",
                "belowType": "STOP_LOSS",
                "belowStopPrice": serialize_price(stop, tick),
                "belowClientOrderId": f"ws-test-{suffix}-sl",
                "newOrderRespType": "FULL",
            }
            with self.session_factory.begin() as db:
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                protection = ProtectiveOrder(
                    live_position_id=position.id,
                    environment="testnet",
                    symbol_id=symbol.id,
                    list_client_order_id=params["listClientOrderId"],
                    stop_client_order_id=params["belowClientOrderId"],
                    take_profit_client_order_id=params["aboveClientOrderId"],
                    quantity=quantity,
                    stop_price=stop,
                    take_profit_price=target,
                    status="submitting",
                    submitted_at=datetime.now(timezone.utc),
                )
                db.add(protection)
                db.flush()
                self._event(db, "protection_submission_started", position, symbol, protection=protection, metadata={"quantity": params["quantity"], "stop_price": params["belowStopPrice"], "take_profit_price": params["abovePrice"]})
                protection_id = protection.id
            response = await client.place_oco_order(params)
            with self.session_factory.begin() as db:
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                protection = db.get(ProtectiveOrder, protection_id)
                orders = response.get("orders", [])
                by_client = {item.get("clientOrderId"): item for item in orders}
                stop_order = by_client.get(protection.stop_client_order_id, {})
                target_order = by_client.get(protection.take_profit_client_order_id, {})
                executing = response.get("listOrderStatus") == "EXECUTING" and bool(stop_order) and bool(target_order)
                protection.order_list_id = str(response.get("orderListId"))
                protection.stop_exchange_order_id = str(stop_order.get("orderId")) if stop_order.get("orderId") is not None else None
                protection.take_profit_exchange_order_id = str(target_order.get("orderId")) if target_order.get("orderId") is not None else None
                protection.raw_status_json = response
                protection.acknowledged_at = datetime.now(timezone.utc)
                protection.status = "protected" if executing else "protection_failed"
                position.protection_status = "protected" if executing else "unprotected"
                event_type = "protection_acknowledged" if executing else "protection_failed"
                self._event(db, event_type, position, symbol, severity="INFO" if executing else "CRITICAL", protection=protection, reason=None if executing else "protective_order_not_executing", metadata={"order_list_id": protection.order_list_id})
                return {"protected": executing, "protection_id": protection.id, "order_list_id": protection.order_list_id, "reason": None if executing else "protective_order_not_executing"}
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            with self.session_factory.begin() as db:
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                protection = db.scalar(select(ProtectiveOrder).where(ProtectiveOrder.live_position_id == position_id).order_by(ProtectiveOrder.id.desc()))
                position.protection_status = "unprotected"
                if protection:
                    protection.status = "protection_failed"
                    protection.rejection_reason = reason
                self._event(db, "protection_failed", position, symbol, severity="CRITICAL", reason=reason, protection=protection)
            return {"protected": False, "reason": reason}
        finally:
            if client:
                await client.close()

    async def reconcile(self, position_id: int) -> dict:
        client = None
        with self.session_factory() as db:
            position = db.get(LivePosition, position_id)
            protection = db.scalar(
                select(ProtectiveOrder)
                .where(
                    ProtectiveOrder.live_position_id == position_id,
                    ProtectiveOrder.order_list_id.is_not(None),
                )
                .order_by(ProtectiveOrder.id.desc())
            )
            if not position or not protection:
                return {"reconciled": False, "reason": "protection_not_found"}
            _, execution_settings = load_stored_settings(db, self.settings)
        try:
            client = self.client_factory(execution_settings)
            response = await client.get_order_list(protection.order_list_id)
            with self.session_factory.begin() as db:
                position = db.get(LivePosition, position_id)
                protection = db.get(ProtectiveOrder, protection.id)
                symbol = db.get(Symbol, position.symbol_id)
                protection.raw_status_json = response
                list_status = response.get("listOrderStatus")
                if list_status == "EXECUTING":
                    protection.status = "protected"
                    position.protection_status = "protected"
                    event_type, severity = "protection_reconciled", "INFO"
                elif position.status == "closed":
                    protection.status = "closed"
                    protection.closed_at = datetime.now(timezone.utc)
                    position.protection_status = "closed"
                    event_type, severity = "protection_reconciled", "INFO"
                else:
                    protection.status = "protection_failed"
                    protection.rejection_reason = f"order_list_{str(list_status).lower()}"
                    position.protection_status = "unprotected"
                    event_type, severity = "protection_failed", "CRITICAL"
                self._event(db, event_type, position, symbol, severity=severity, protection=protection, reason=protection.rejection_reason, metadata={"order_list_id": protection.order_list_id, "list_order_status": list_status})
                return {"reconciled": True, "protected": position.protection_status == "protected", "status": list_status}
        except Exception as exc:
            return {"reconciled": False, "reason": str(exc) or type(exc).__name__}
        finally:
            if client:
                await client.close()


spot_protection_service = SpotProtectionService()
