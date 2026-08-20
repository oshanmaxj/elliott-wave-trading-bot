from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.execution.binance import BinanceError, BinanceSpotClient
from app.execution.credentials import load_stored_settings
from app.execution.filters import (
    floor_quantity_to_step,
    quantity_limits,
    serialize_quantity,
    validate_notional,
    validate_symbol_tradeability,
)
from app.models import (
    ExecutionEvent,
    ExecutionFill,
    ExecutionOrder,
    LivePosition,
    ProtectiveOrder,
    Symbol,
)


class ManualCloseError(RuntimeError):
    def __init__(self, reason: str, status_code: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class ManualPositionCloseService:
    """Testnet-only, position-scoped reduction of an existing Spot long."""

    def __init__(self, session_factory=SessionLocal, client_factory=BinanceSpotClient,
                 settings=None):
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.settings = settings or get_settings()

    def _event(self, db, event_type, position, symbol, actor, *, order=None,
               severity="INFO", reason=None, requested=None, executed=None):
        now = datetime.now(timezone.utc)
        db.add(ExecutionEvent(
            severity=severity, event_type=event_type, exchange="binance",
            environment=position.environment, symbol_id=symbol.id,
            trade_setup_id=position.originating_trade_setup_id,
            execution_order_id=order.id if order else None,
            live_position_id=position.id,
            message=reason or event_type.replace("_", " "),
            metadata_json={
                "position_id": position.id,
                "trade_setup_id": position.originating_trade_setup_id,
                "symbol": symbol.symbol,
                "requested_quantity": str(requested) if requested is not None else None,
                "executed_quantity": str(executed) if executed is not None else None,
                "remaining_quantity": str(position.remaining_quantity),
                "exchange_order_id": order.exchange_order_id if order else None,
                "actor": actor, "timestamp": now.isoformat(), "reason": reason,
            },
        ))

    def _active_close(self, db, position):
        prefix = f"ws-test-pos-{position.id}-close-"
        return db.scalar(select(ExecutionOrder).where(
            ExecutionOrder.trade_setup_id == position.originating_trade_setup_id,
            ExecutionOrder.client_order_id.startswith(prefix),
            ExecutionOrder.execution_state.in_(["submitting", "unknown", "acknowledged"]),
        ).order_by(ExecutionOrder.id.desc()))

    async def close_position(self, position_id: int, actor: str) -> dict:
        client = None
        with self.session_factory.begin() as db:
            position = db.scalar(select(LivePosition).where(
                LivePosition.id == position_id).with_for_update())
            if not position:
                raise ManualCloseError("position_not_found", 404)
            if position.status == "closed" or position.remaining_quantity <= 0:
                raise ManualCloseError("position_already_closed")
            if position.environment != "testnet" or self.settings.binance_environment != "testnet":
                raise ManualCloseError("manual_close_is_testnet_only", 423)
            if position.direction != "long":
                raise ManualCloseError("manual_close_only_supports_spot_long")
            symbol = db.get(Symbol, position.symbol_id)
            active = self._active_close(db, position)
            if active:
                return {"submitted": False, "duplicate": True, "order_id": active.id,
                        "status": active.status, "position_id": position.id}
            previous = list(db.scalars(select(ExecutionOrder).where(
                ExecutionOrder.client_order_id.startswith(
                    f"ws-test-pos-{position.id}-close-"))))
            cid = f"ws-test-pos-{position.id}-close-{len(previous) + 1}"
            order = ExecutionOrder(
                environment="testnet", symbol_id=symbol.id,
                trade_setup_id=position.originating_trade_setup_id,
                client_order_id=cid, side="SELL", order_type="MARKET",
                requested_quantity=position.remaining_quantity,
                executed_quantity=Decimal("0"), status="submitting",
                execution_state="submitting", submitted_at=datetime.now(timezone.utc),
            )
            db.add(order)
            db.flush()
            self._event(db, "manual_position_close_requested", position, symbol,
                        actor, order=order, requested=position.remaining_quantity)
            order_id = order.id

        try:
            with self.session_factory() as db:
                _, configured = load_stored_settings(db, self.settings)
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                protections = list(db.scalars(select(ProtectiveOrder).where(
                    ProtectiveOrder.live_position_id == position_id,
                    ProtectiveOrder.status.in_(["submitting", "protected"]),
                )))
            client = self.client_factory(configured)

            for protection in protections:
                if not protection.order_list_id:
                    raise ManualCloseError("protective_order_state_unknown")
                try:
                    canceled = await client.cancel_order_list(
                        symbol.symbol, protection.order_list_id)
                except Exception as exc:
                    raise ManualCloseError(f"protection_cancellation_failed: {exc}") from exc
                if (
                    canceled.get("listOrderStatus") != "ALL_DONE"
                    and canceled.get("listStatusType") != "ALL_DONE"
                ):
                    raise ManualCloseError("protection_cancellation_not_acknowledged")
                with self.session_factory.begin() as db:
                    stored = db.get(ProtectiveOrder, protection.id)
                    stored.status = "closed"
                    stored.closed_at = datetime.now(timezone.utc)
                    stored.raw_status_json = canceled
                    db.get(LivePosition, position_id).protection_status = "unprotected"

            info = (await client.exchange_info(symbol.symbol))["symbols"][0]
            tradeability = validate_symbol_tradeability(info, "MARKET")
            if tradeability:
                raise ManualCloseError(tradeability[0])
            account = await client.account()
            free = next((Decimal(row["free"]) for row in account.get("balances", [])
                         if row["asset"] == symbol.base_asset), Decimal("0"))
            with self.session_factory() as db:
                remaining = Decimal(db.get(LivePosition, position_id).remaining_quantity)
            minimum, maximum, step = quantity_limits(info, market=True)
            if step <= 0:
                raise ManualCloseError("invalid_market_quantity_step")
            quantity = floor_quantity_to_step(min(remaining, free), step)
            if quantity <= 0 or quantity < minimum:
                raise ManualCloseError("insufficient_reconciled_quantity")
            if quantity > maximum:
                quantity = floor_quantity_to_step(maximum, step)
            price = Decimal((await client.ticker_price(symbol.symbol))["price"])
            notional = validate_notional(price, quantity, info)
            if notional:
                raise ManualCloseError(notional[0])

            params = {"symbol": symbol.symbol, "side": "SELL", "type": "MARKET",
                      "quantity": serialize_quantity(quantity, step),
                      "newClientOrderId": cid, "newOrderRespType": "FULL"}
            with self.session_factory.begin() as db:
                order = db.get(ExecutionOrder, order_id)
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                order.requested_quantity = quantity
                self._event(db, "manual_position_close_order_submitted", position,
                            symbol, actor, order=order, requested=quantity,
                            executed=Decimal("0"))
            try:
                response = await client.place_order(params)
            except BinanceError as exc:
                if not exc.unknown:
                    raise
                response = await client.get_order(symbol.symbol, cid)
            await self._apply_response(order_id, response, actor)
            with self.session_factory() as db:
                order = db.get(ExecutionOrder, order_id)
                position = db.get(LivePosition, position_id)
                return {"submitted": True, "duplicate": False,
                        "position_id": position.id, "position_status": position.status,
                        "remaining_quantity": str(position.remaining_quantity),
                        "order_id": order.id, "exchange_order_id": order.exchange_order_id,
                        "status": order.status, "executed_quantity": str(order.executed_quantity)}
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ManualCloseError) else str(exc) or type(exc).__name__
            with self.session_factory.begin() as db:
                order = db.get(ExecutionOrder, order_id)
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                order.status = "execution_unknown" if isinstance(exc, BinanceError) and exc.unknown else "exchange_rejected"
                order.execution_state = "unknown" if order.status == "execution_unknown" else "rejected"
                order.rejection_reason = reason
                self._event(db, "manual_position_close_failed", position, symbol,
                            actor, order=order, severity="CRITICAL", reason=reason,
                            requested=order.requested_quantity, executed=order.executed_quantity)
            if isinstance(exc, ManualCloseError):
                raise
            raise ManualCloseError(reason, 503 if isinstance(exc, BinanceError) and exc.unknown else 409) from exc
        finally:
            if client:
                await client.close()

    async def _apply_response(self, order_id, response, actor):
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as db:
            order = db.get(ExecutionOrder, order_id)
            position = db.scalar(select(LivePosition).where(
                LivePosition.originating_trade_setup_id == order.trade_setup_id,
                LivePosition.environment == order.environment,
            ).order_by(LivePosition.id.desc()).with_for_update())
            symbol = db.get(Symbol, order.symbol_id)
            executed = Decimal(response.get("executedQty", "0"))
            quote = Decimal(response.get("cummulativeQuoteQty", "0"))
            order.exchange_order_id = str(response.get("orderId")) if response.get("orderId") is not None else None
            order.status = response.get("status", "UNKNOWN")
            order.execution_state = (
                "filled" if order.status == "FILLED"
                else "partially_filled" if executed > 0
                else "acknowledged"
            )
            order.executed_quantity = executed
            order.quote_quantity = quote
            order.average_fill_price = quote / executed if executed else None
            order.raw_status_json = response
            order.acknowledged_at = now
            if order.status == "FILLED":
                order.filled_at = now
            if db.scalar(select(ExecutionFill.id).where(
                ExecutionFill.execution_order_id == order.id).limit(1)):
                if position.status == "closed":
                    position.exit_reason = "manual_close"
                    position.exit_price = order.average_fill_price
                    event_type = "manual_position_closed"
                else:
                    event_type = "manual_position_close_partial"
                self._event(db, event_type, position, symbol, actor, order=order,
                            requested=order.requested_quantity, executed=executed)
                return
            sold = min(executed, Decimal(position.remaining_quantity))
            fees = Decimal("0")
            for index, fill in enumerate(response.get("fills", [])):
                price, quantity = Decimal(fill["price"]), Decimal(fill["qty"])
                commission = Decimal(fill.get("commission", "0"))
                commission_asset = fill.get("commissionAsset", "")
                if commission_asset == symbol.quote_asset:
                    fees += commission
                db.add(ExecutionFill(
                    execution_order_id=order.id,
                    exchange_trade_id=str(fill.get("tradeId", f"response-{index}")),
                    price=price, quantity=quantity, quote_quantity=price * quantity,
                    commission=commission, commission_asset=commission_asset,
                    filled_at=now,
                ))
            position.realized_pnl += (
                (order.average_fill_price - position.average_entry) * sold - fees
                if order.average_fill_price is not None else Decimal("0")
            )
            position.total_fees += fees
            position.remaining_quantity -= sold
            if sold > 0:
                self._event(db, "manual_position_close_filled", position, symbol,
                            actor, order=order, requested=order.requested_quantity,
                            executed=executed)
            if position.remaining_quantity <= 0:
                position.remaining_quantity = Decimal("0")
                position.status = "closed"
                position.closed_at = now
                position.exit_reason = "manual_close"
                position.exit_price = order.average_fill_price
                position.protection_status = "closed"
                event_type = "manual_position_closed"
            else:
                position.status = "partially_closed" if sold > 0 else "open"
                event_type = "manual_position_close_partial"
            self._event(db, event_type, position, symbol, actor, order=order,
                        requested=order.requested_quantity, executed=executed)


manual_position_close_service = ManualPositionCloseService()
