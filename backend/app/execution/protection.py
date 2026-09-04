from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
from app.execution.runtime import runtime_state
from app.models import (
    ExecutionEvent,
    ExecutionOrder,
    LivePosition,
    ProtectiveOrder,
    Symbol,
    TradeSetup,
)
from app.trading.paper_forward import TP_FRACTIONS

TP_FRACTION_SUM_TOLERANCE = Decimal("0.5")


def ordered_targets(setup) -> list[tuple[int, Decimal]]:
    """Return the setup's non-null take-profit targets in order, e.g. [(1, tp1), (2, tp2)]."""
    targets = []
    for number in (1, 2, 3):
        value = getattr(setup, f"take_profit_{number}")
        if value is not None:
            targets.append((number, value))
    return targets


def resolve_tp_fractions(runtime) -> tuple[dict[int, Decimal], bool, str | None]:
    """Build per-account TP1/TP2/TP3 slice fractions from risk_config_json.

    Falls back to the paper-forward simulator's fixed 30/40/30 baseline
    (`app.trading.paper_forward.TP_FRACTIONS`) whenever the account's
    tp1_pct/tp2_pct/tp3_pct are missing, unparsable, or don't sum to ~100 —
    live and paper-forward can diverge once an account sets a valid custom
    split, which is intentional.
    """
    raw = (runtime.risk_config_json if runtime else {}) or {}
    try:
        tp1 = Decimal(str(raw.get("tp1_pct", "")))
        tp2 = Decimal(str(raw.get("tp2_pct", "")))
        tp3 = Decimal(str(raw.get("tp3_pct", "")))
    except (InvalidOperation, TypeError, ValueError):
        return dict(TP_FRACTIONS), True, "tp_fraction_config_unparsable"
    total = tp1 + tp2 + tp3
    if abs(total - Decimal("100")) > TP_FRACTION_SUM_TOLERANCE:
        return dict(TP_FRACTIONS), True, "tp_fraction_config_not_100_pct"
    if tp1 < 0 or tp2 < 0 or tp3 < 0:
        return dict(TP_FRACTIONS), True, "tp_fraction_config_negative"
    hundred = Decimal("100")
    return {1: tp1 / hundred, 2: tp2 / hundred, 3: tp3 / hundred}, False, None


class SpotProtectionService:
    """Protect a filled Spot BUY with scaled 30/40/30 TP1/TP2/TP3 brackets and breakeven-after-TP1."""

    def __init__(
        self,
        session_factory=SessionLocal,
        client_factory=BinanceSpotClient,
        settings=None,
    ):
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.settings = settings or get_settings()

    def _event(
        self,
        db,
        event_type,
        position,
        symbol,
        *,
        severity="INFO",
        reason=None,
        protection=None,
        metadata=None,
    ):
        execution = db.scalar(
            select(ExecutionOrder)
            .where(ExecutionOrder.trade_setup_id == position.originating_trade_setup_id)
            .order_by(ExecutionOrder.id.desc())
        )
        details = {
            "symbol": symbol.symbol,
            "setup_id": position.originating_trade_setup_id,
            "execution_id": execution.id if execution else None,
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
                execution_order_id=execution.id if execution else None,
                live_position_id=position.id,
                message=reason or event_type.replace("_", " "),
                metadata_json=details,
            )
        )
        log_event(
            severity,
            "protection",
            event_type,
            reason or event_type.replace("_", " "),
            details,
        )

    async def establish(self, position_id: int) -> dict:
        client = None
        params = None
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
                return {
                    "protected": existing.status == "protected",
                    "reason": "protection_already_exists",
                    "protection_id": existing.id,
                }
            setup = db.get(TradeSetup, position.originating_trade_setup_id)
            symbol = db.get(Symbol, position.symbol_id)
            remaining_targets = (
                ordered_targets(setup)[position.protection_stage :] if setup else []
            )
            reason = None
            if not setup or setup.stop_loss is None:
                reason = "missing_protective_stop"
            elif not remaining_targets:
                reason = "missing_protective_target"
            elif (
                position.direction == "long"
                and not (
                    Decimal(position.stop_loss)
                    <= position.average_entry
                    < Decimal(remaining_targets[0][1])
                )
            ):
                reason = "invalid_protective_geometry"
            elif (
                position.direction == "short"
                and not (
                    Decimal(remaining_targets[0][1])
                    < position.average_entry
                    <= Decimal(position.stop_loss)
                )
            ):
                reason = "invalid_protective_geometry"
            elif position.direction not in {"long", "short"}:
                reason = "invalid_position_direction"
            elif position.remaining_quantity <= 0:
                reason = "invalid_protective_quantity"
            if reason:
                position.protection_status = "unprotected"
                self._event(
                    db,
                    "protection_failed",
                    position,
                    symbol,
                    severity="CRITICAL",
                    reason=reason,
                )
                db.commit()
                return {"protected": False, "reason": reason}
            target_number, raw_target_price = remaining_targets[0]
            stage = position.protection_stage
            position.protection_status = "protection_pending"
            self._event(
                db,
                "protection_pending",
                position,
                symbol,
                metadata={
                    "raw_stop_price": str(position.stop_loss),
                    "raw_take_profit_price": str(raw_target_price),
                    "raw_remaining_quantity": str(position.remaining_quantity),
                    "target_number": target_number,
                    "protection_stage": stage,
                },
            )
            db.commit()

        try:
            with self.session_factory() as db:
                _, execution_settings = load_stored_settings(db, self.settings)
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                setup = db.get(TradeSetup, position.originating_trade_setup_id)
                remaining_targets = ordered_targets(setup)[position.protection_stage :]
                target_number, raw_target_price = remaining_targets[0]
                stage = position.protection_stage
                multi_target = len(remaining_targets) > 1
                symbol_name = symbol.symbol
                tp_fractions, tp_fallback_used, tp_fallback_reason = resolve_tp_fractions(
                    runtime_state(db)
                )
            client = self.client_factory(execution_settings)
            info = (await client.exchange_info(symbol_name))["symbols"][0]
            account = await client.account()
            base_balance = next(
                (
                    Decimal(row["free"])
                    for row in account.get("balances", [])
                    if row["asset"] == symbol.base_asset
                ),
                Decimal("0"),
            )
            minimum, maximum, step = quantity_limits(info, market=False)
            tick = price_tick(info)
            if step <= 0:
                raise ValueError("invalid_protective_quantity_step")
            if tick <= 0:
                raise ValueError("invalid_protective_price_tick")
            sellable = min(Decimal(position.remaining_quantity), base_balance)
            stop = floor_quantity_to_step(Decimal(position.stop_loss), tick)
            target = floor_quantity_to_step(Decimal(raw_target_price), tick)
            if position.direction == "long" and not (
                stop <= position.average_entry < target
            ):
                raise ValueError("invalid_protective_geometry")
            if position.direction == "short" and not (
                target < position.average_entry <= stop
            ):
                raise ValueError("invalid_protective_geometry")

            def within_limits(qty: Decimal) -> bool:
                return qty > 0 and minimum <= qty <= maximum

            quantity = floor_quantity_to_step(sellable, step)
            guard_quantity = Decimal("0")
            if multi_target:
                slice_qty = floor_quantity_to_step(
                    min(sellable, Decimal(position.base_quantity) * tp_fractions[target_number]),
                    step,
                )
                remainder_qty = floor_quantity_to_step(sellable - slice_qty, step)
                slice_ok = (
                    within_limits(slice_qty)
                    and not validate_notional(stop, slice_qty, info)
                    and not validate_notional(target, slice_qty, info)
                )
                remainder_ok = within_limits(remainder_qty) and not validate_notional(
                    stop, remainder_qty, info
                )
                if slice_ok and remainder_ok:
                    quantity = slice_qty
                    guard_quantity = remainder_qty
            if not within_limits(quantity):
                raise ValueError("invalid_protective_quantity")
            notional_reasons = list(
                dict.fromkeys(
                    validate_notional(stop, quantity, info)
                    + validate_notional(target, quantity, info)
                )
            )
            if notional_reasons:
                raise ValueError(notional_reasons[0])

            suffix = f"{position.id}-{stage}"
            long_position = position.direction == "long"
            tp_label = f"tp{target_number}"
            params = {
                "symbol": symbol_name,
                "side": "SELL" if long_position else "BUY",
                "quantity": serialize_quantity(quantity, step),
                "listClientOrderId": f"ws-test-{suffix}-protect",
                "aboveType": "LIMIT_MAKER" if long_position else "STOP_LOSS",
                ("abovePrice" if long_position else "aboveStopPrice"): serialize_price(
                    target if long_position else stop, tick
                ),
                "aboveClientOrderId": f"ws-test-{suffix}-{tp_label if long_position else 'sl'}",
                "belowType": "STOP_LOSS" if long_position else "LIMIT_MAKER",
                ("belowStopPrice" if long_position else "belowPrice"): serialize_price(
                    stop if long_position else target, tick
                ),
                "belowClientOrderId": f"ws-test-{suffix}-{'sl' if long_position else tp_label}",
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
                    stop_client_order_id=params[
                        "belowClientOrderId" if long_position else "aboveClientOrderId"
                    ],
                    take_profit_client_order_id=params[
                        "aboveClientOrderId" if long_position else "belowClientOrderId"
                    ],
                    quantity=quantity,
                    stop_price=stop,
                    take_profit_price=target,
                    stage=stage,
                    role="bracket",
                    status="submitting",
                    submitted_at=datetime.now(timezone.utc),
                )
                db.add(protection)
                db.flush()
                self._event(
                    db,
                    "protection_submission_started",
                    position,
                    symbol,
                    protection=protection,
                    metadata={"attempted_parameters": params},
                )
                if tp_fallback_used and multi_target:
                    self._event(
                        db,
                        "tp_fraction_config_invalid",
                        position,
                        symbol,
                        severity="WARNING",
                        protection=protection,
                        reason=tp_fallback_reason,
                        metadata={
                            "fallback_fractions": {
                                k: str(v) for k, v in TP_FRACTIONS.items()
                            }
                        },
                    )
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
                order_list_id = response.get("orderListId")
                executing = (
                    response.get("listOrderStatus") == "EXECUTING"
                    and order_list_id is not None
                    and bool(stop_order.get("orderId"))
                    and bool(target_order.get("orderId"))
                )
                protection.order_list_id = (
                    str(order_list_id) if order_list_id is not None else None
                )
                protection.stop_exchange_order_id = (
                    str(stop_order.get("orderId"))
                    if stop_order.get("orderId") is not None
                    else None
                )
                protection.take_profit_exchange_order_id = (
                    str(target_order.get("orderId"))
                    if target_order.get("orderId") is not None
                    else None
                )
                protection.raw_status_json = response
                protection.acknowledged_at = datetime.now(timezone.utc)
                protection.status = "protected" if executing else "protection_failed"
                position.protection_status = "protected" if executing else "unprotected"
                if executing:
                    position.stop_loss = protection.stop_price
                event_type = (
                    "protection_acknowledged" if executing else "protection_failed"
                )
                self._event(
                    db,
                    event_type,
                    position,
                    symbol,
                    severity="INFO" if executing else "CRITICAL",
                    protection=protection,
                    reason=None if executing else "protective_order_not_executing",
                    metadata={
                        "order_list_id": protection.order_list_id,
                        "raw_quantity": str(position.remaining_quantity),
                        "sellable_balance": str(base_balance),
                        "normalized_quantity": str(quantity),
                        "raw_stop_price": str(position.stop_loss),
                        "normalized_stop_price": str(stop),
                        "raw_take_profit_price": str(raw_target_price),
                        "normalized_take_profit_price": str(target),
                        "stop_order_id": protection.stop_exchange_order_id,
                        "take_profit_order_id": protection.take_profit_exchange_order_id,
                        "guard_quantity": str(guard_quantity),
                        "target_number": target_number,
                        "protection_stage": stage,
                    },
                )
                order_list_id_str = protection.order_list_id
                bracket_protected = executing

            if not bracket_protected:
                return {
                    "protected": False,
                    "protection_id": protection_id,
                    "order_list_id": order_list_id_str,
                    "reason": "protective_order_not_executing",
                }

            if guard_quantity > 0:
                order_types = info.get("orderTypes", [])
                guard_client_order_id = f"ws-test-{suffix}-guard"
                try:
                    guard_response = await self._place_guard(
                        client,
                        symbol_name=symbol_name,
                        long_position=long_position,
                        quantity=guard_quantity,
                        stop=stop,
                        tick=tick,
                        step=step,
                        order_types=order_types,
                        client_order_id=guard_client_order_id,
                    )
                except Exception as guard_exc:
                    guard_reason = str(guard_exc) or type(guard_exc).__name__
                    with self.session_factory.begin() as db:
                        position = db.get(LivePosition, position_id)
                        symbol = db.get(Symbol, position.symbol_id)
                        protection = db.get(ProtectiveOrder, protection_id)
                        try:
                            await client.cancel_order_list(
                                symbol.symbol, protection.order_list_id
                            )
                        except Exception:
                            pass
                        protection.status = "protection_failed"
                        protection.rejection_reason = f"guard_order_failed:{guard_reason}"
                        position.protection_status = "unprotected"
                        self._event(
                            db,
                            "protection_failed",
                            position,
                            symbol,
                            severity="CRITICAL",
                            protection=protection,
                            reason="guard_order_failed",
                            metadata={
                                "guard_error": guard_reason,
                                "order_list_id": protection.order_list_id,
                            },
                        )
                    return {
                        "protected": False,
                        "protection_id": protection_id,
                        "reason": "guard_order_failed",
                    }

                guard_order_id = guard_response.get("orderId")
                with self.session_factory.begin() as db:
                    position = db.get(LivePosition, position_id)
                    symbol = db.get(Symbol, position.symbol_id)
                    guard_protection = ProtectiveOrder(
                        live_position_id=position.id,
                        environment="testnet",
                        symbol_id=symbol.id,
                        list_client_order_id=guard_client_order_id,
                        stop_client_order_id=guard_client_order_id,
                        take_profit_client_order_id=None,
                        stop_exchange_order_id=str(guard_order_id)
                        if guard_order_id is not None
                        else None,
                        take_profit_exchange_order_id=None,
                        quantity=guard_quantity,
                        stop_price=stop,
                        take_profit_price=None,
                        stage=stage,
                        role="guard_stop",
                        status="protected",
                        raw_status_json=guard_response,
                        submitted_at=datetime.now(timezone.utc),
                        acknowledged_at=datetime.now(timezone.utc),
                    )
                    db.add(guard_protection)
                    db.flush()
                    self._event(
                        db,
                        "guard_order_placed",
                        position,
                        symbol,
                        protection=guard_protection,
                        metadata={
                            "guard_quantity": str(guard_quantity),
                            "stop_price": str(stop),
                        },
                    )

            return {
                "protected": True,
                "protection_id": protection_id,
                "order_list_id": order_list_id_str,
                "reason": None,
            }
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            with self.session_factory.begin() as db:
                position = db.get(LivePosition, position_id)
                symbol = db.get(Symbol, position.symbol_id)
                protection = db.scalar(
                    select(ProtectiveOrder)
                    .where(ProtectiveOrder.live_position_id == position_id)
                    .order_by(ProtectiveOrder.id.desc())
                )
                position.protection_status = "unprotected"
                if protection:
                    protection.status = "protection_failed"
                    protection.rejection_reason = reason
                self._event(
                    db,
                    "protection_failed",
                    position,
                    symbol,
                    severity="CRITICAL",
                    reason=reason,
                    protection=protection,
                    metadata={
                        "attempted_parameters": params,
                        "binance_error": reason,
                    },
                )
            return {"protected": False, "reason": reason}
        finally:
            if client:
                await client.close()

    async def _place_guard(
        self,
        client,
        *,
        symbol_name,
        long_position,
        quantity,
        stop,
        tick,
        step,
        order_types,
        client_order_id,
    ) -> dict:
        order_type = "STOP_LOSS" if "STOP_LOSS" in order_types else "STOP_LOSS_LIMIT"
        params = {
            "symbol": symbol_name,
            "side": "SELL" if long_position else "BUY",
            "type": order_type,
            "quantity": serialize_quantity(quantity, step),
            "stopPrice": serialize_price(stop, tick),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        if order_type == "STOP_LOSS_LIMIT":
            params["price"] = serialize_price(stop, tick)
            params["timeInForce"] = "GTC"
        return await client.place_order(params)

    async def advance_stage(self, position_id: int) -> dict:
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as db:
            position = db.get(LivePosition, position_id)
            if not position:
                return {"advanced": False, "reason": "position_not_found"}
            symbol = db.get(Symbol, position.symbol_id)
            stage = position.protection_stage
            bracket = db.scalar(
                select(ProtectiveOrder)
                .where(
                    ProtectiveOrder.live_position_id == position.id,
                    ProtectiveOrder.role == "bracket",
                    ProtectiveOrder.stage == stage,
                )
                .order_by(ProtectiveOrder.id.desc())
            )
            if bracket and bracket.status != "closed":
                bracket.status = "closed"
                bracket.closed_at = now
            target_number = stage + 1
            if target_number in (1, 2, 3):
                setattr(position, f"tp{target_number}_filled_at", now)
            moved_to_breakeven = False
            if stage == 0:
                position.stop_loss = position.average_entry
                position.breakeven_moved_at = now
                moved_to_breakeven = True
            position.protection_stage = stage + 1
            guard = db.scalar(
                select(ProtectiveOrder).where(
                    ProtectiveOrder.live_position_id == position.id,
                    ProtectiveOrder.role == "guard_stop",
                    ProtectiveOrder.stage == stage,
                    ProtectiveOrder.status == "protected",
                )
            )
            guard_id = guard.id if guard else None
            symbol_name = symbol.symbol
            _, execution_settings = load_stored_settings(db, self.settings)
            remaining = Decimal(position.remaining_quantity)
            closed = position.status == "closed"
            self._event(
                db,
                "protection_stage_advanced",
                position,
                symbol,
                protection=bracket,
                metadata={
                    "completed_stage": stage,
                    "next_stage": position.protection_stage,
                    "moved_to_breakeven": moved_to_breakeven,
                },
            )

        if guard_id:
            client = self.client_factory(execution_settings)
            try:
                with self.session_factory() as db:
                    guard = db.get(ProtectiveOrder, guard_id)
                    stop_client_order_id = guard.stop_client_order_id
                await client.cancel_order(symbol_name, stop_client_order_id)
                with self.session_factory.begin() as db:
                    guard = db.get(ProtectiveOrder, guard_id)
                    guard.status = "closed"
                    guard.closed_at = datetime.now(timezone.utc)
            except Exception as exc:
                log_event(
                    "WARNING",
                    "protection",
                    "guard_cancel_failed",
                    str(exc) or type(exc).__name__,
                    {"position_id": position_id, "guard_id": guard_id},
                )
            finally:
                await client.close()

        result = {"advanced": True, "moved_to_breakeven": moved_to_breakeven}
        if remaining > 0 and not closed:
            result["establish"] = await self.establish(position_id)
        return result

    async def reconcile(self, position_id: int) -> dict:
        client = None
        with self.session_factory() as db:
            position = db.get(LivePosition, position_id)
            protection = db.scalar(
                select(ProtectiveOrder)
                .where(
                    ProtectiveOrder.live_position_id == position_id,
                    ProtectiveOrder.role == "bracket",
                )
                .order_by(ProtectiveOrder.id.desc())
            )
            if not position or not protection or not protection.order_list_id:
                return {"reconciled": False, "reason": "protection_not_found"}
            if protection.status != "protected":
                return {"reconciled": False, "reason": "bracket_not_protected"}
            symbol = db.get(Symbol, position.symbol_id)
            symbol_name = symbol.symbol
            _, execution_settings = load_stored_settings(db, self.settings)
        try:
            client = self.client_factory(execution_settings)
            response = await client.get_order_list(protection.order_list_id)
            list_status = response.get("listOrderStatus")
            if list_status == "EXECUTING":
                return {"reconciled": True, "protected": True, "status": list_status}
            if list_status != "ALL_DONE":
                with self.session_factory.begin() as db:
                    position = db.get(LivePosition, position_id)
                    protection = db.get(ProtectiveOrder, protection.id)
                    symbol = db.get(Symbol, position.symbol_id)
                    protection.raw_status_json = response
                    protection.status = "protection_failed"
                    protection.rejection_reason = (
                        f"order_list_{str(list_status).lower()}"
                    )
                    position.protection_status = "unprotected"
                    self._event(
                        db,
                        "protection_failed",
                        position,
                        symbol,
                        severity="CRITICAL",
                        protection=protection,
                        reason=protection.rejection_reason,
                        metadata={
                            "order_list_id": protection.order_list_id,
                            "list_order_status": list_status,
                        },
                    )
                return {"reconciled": True, "protected": False, "status": list_status}

            tp_detail = await client.get_order(
                symbol_name, protection.take_profit_client_order_id
            )
            stop_detail = await client.get_order(
                symbol_name, protection.stop_client_order_id
            )
            tp_filled = tp_detail.get("status") == "FILLED"

            with self.session_factory.begin() as db:
                position = db.get(LivePosition, position_id)
                protection = db.get(ProtectiveOrder, protection.id)
                symbol = db.get(Symbol, position.symbol_id)
                protection.raw_status_json = response
                protection.status = "closed"
                protection.closed_at = datetime.now(timezone.utc)
                self._event(
                    db,
                    "protection_reconciled",
                    position,
                    symbol,
                    protection=protection,
                    metadata={
                        "order_list_id": protection.order_list_id,
                        "list_order_status": list_status,
                        "take_profit_status": tp_detail.get("status"),
                        "stop_status": stop_detail.get("status"),
                    },
                )
                remaining = Decimal(position.remaining_quantity)
                position_closed = position.status == "closed"

            if tp_filled and remaining > 0 and not position_closed:
                advance_result = await self.advance_stage(position_id)
                return {
                    "reconciled": True,
                    "protected": True,
                    "status": list_status,
                    "advanced": advance_result,
                }
            with self.session_factory() as db:
                protected = (
                    db.get(LivePosition, position_id).protection_status == "protected"
                )
            return {"reconciled": True, "protected": protected, "status": list_status}
        except Exception as exc:
            return {"reconciled": False, "reason": str(exc) or type(exc).__name__}
        finally:
            if client:
                await client.close()


spot_protection_service = SpotProtectionService()
