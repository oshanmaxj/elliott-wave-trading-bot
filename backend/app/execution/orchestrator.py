from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.execution.binance import BinanceError, BinanceSpotClient
from app.execution.credentials import load_stored_settings
from app.execution.service import (
    ExecutionRiskEngine,
    client_order_id,
    setup_fingerprint,
)
from app.execution.strategies import originating_runtime_strategy
from app.execution.filters import quantity_limits, serialize_quantity
from app.models import (
    BotRuntimeState,
    DailyRiskLedger,
    ExecutionEvent,
    ExecutionOrder,
    LivePosition,
    Symbol,
    TradeSetup,
)


class AutomaticTestnetExecutor:
    """Idempotent, Spot-Testnet-only handoff for newly eligible setups."""

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
        setup,
        symbol,
        *,
        order=None,
        reason=None,
        severity="INFO",
        metadata=None,
    ):
        details = {
            "symbol": symbol.symbol,
            "timeframe": setup.setup_timeframe,
            "strategy": setup.strategy,
            "side": "BUY" if setup.direction == "bullish" else "SELL",
            "reason": reason,
            "client_order_id": order.client_order_id if order else None,
            **(metadata or {}),
        }
        db.add(
            ExecutionEvent(
                severity=severity,
                event_type=event_type,
                exchange="binance",
                environment="testnet",
                symbol_id=symbol.id,
                trade_setup_id=setup.id,
                execution_order_id=order.id if order else None,
                message=reason or event_type.replace("_", " "),
                metadata_json=details,
            )
        )
        log_event(
            severity,
            "execution",
            event_type,
            reason or event_type.replace("_", " "),
            {
                "setup_id": setup.id,
                "execution_order_id": order.id if order else None,
                **details,
            },
        )

    def _preflight_reasons(self, db, setup, symbol, manual_approved=False):
        runtime = db.scalar(select(BotRuntimeState).limit(1))
        reasons = []
        if (
            self.settings.binance_environment != "testnet"
            or not self.settings.binance_execution_enabled
        ):
            reasons.append("automatic_testnet_not_enabled")
        if self.settings.allow_production_orders:
            reasons.append("production_orders_must_remain_locked")
        if not runtime or runtime.status != "running":
            reasons.append("bot_not_running")
        elif runtime.pause_new_entries:
            reasons.append("new_entries_paused")
        if runtime and runtime.kill_switch_enabled:
            reasons.append("kill_switch_enabled")
        if runtime and not manual_approved and not runtime.automatic_trading_enabled:
            reasons.append("automatic_trading_disabled")
        if runtime and not manual_approved and runtime.manual_approval_required:
            reasons.append("manual_approval_required")
        if runtime and manual_approved and not runtime.manual_approval_required:
            reasons.append("manual_approval_not_enabled")
        if runtime and symbol.symbol not in runtime.enabled_symbols_json:
            reasons.append("symbol_not_enabled")
        if runtime and setup.setup_timeframe not in runtime.enabled_timeframes_json:
            reasons.append("timeframe_not_enabled")
        if db.scalar(
            select(LivePosition.id)
            .where(
                LivePosition.symbol_id == symbol.id,
                LivePosition.status.in_(["open", "partially_closed"]),
                LivePosition.protection_status != "protected",
            )
            .limit(1)
        ):
            reasons.append("symbol_has_unprotected_position")
        if setup.status not in {
            "ready",
            "eligible",
            "approved",
            "pending_approval",
            "triggered",
        }:
            reasons.append("setup_not_eligible")
        expires = setup.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            reasons.append("setup_expired")
        if setup.rejection_reasons_json:
            reasons.append("setup_rejected")
        if setup.strategy not in self.settings.allowed_execution_strategies:
            reasons.append("execution_strategy_not_allowed")
        origin = originating_runtime_strategy(setup)
        if not origin:
            reasons.append("strategy_mapping_missing")
        elif runtime and origin not in runtime.enabled_strategies_json:
            reasons.append("originating_strategy_not_enabled")
        if setup.direction != "bullish":
            reasons.append("spot_sell_requires_asset_balance")
        ledger = db.scalar(
            select(DailyRiskLedger)
            .where(
                DailyRiskLedger.exchange == "binance",
                DailyRiskLedger.environment == "testnet",
            )
            .order_by(DailyRiskLedger.created_at.desc())
        )
        if ledger and ledger.kill_switch_triggered:
            reasons.append("kill_switch_enabled")
        if ledger and ledger.loss_pct >= self.settings.max_daily_loss_pct:
            reasons.append("daily_loss_limit")
        return list(dict.fromkeys(reasons))

    async def handoff(self, setup_id: int, *, manual_approved=False) -> dict:
        client = None
        with self.session_factory() as db:
            setup = db.scalar(
                select(TradeSetup).where(TradeSetup.id == setup_id).with_for_update()
            )
            if not setup:
                return {"started": False, "reason": "setup_not_found"}
            symbol = db.get(Symbol, setup.symbol_id)
            existing = db.scalar(
                select(ExecutionOrder).where(ExecutionOrder.trade_setup_id == setup.id)
            )
            if existing:
                return {
                    "started": False,
                    "reason": "duplicate_setup_window",
                    "order_id": existing.id,
                }
            reasons = self._preflight_reasons(db, setup, symbol, manual_approved)
            if reasons:
                self._event(
                    db,
                    "auto_execution_skipped",
                    setup,
                    symbol,
                    reason=reasons[0],
                    metadata={"reasons": reasons},
                )
                db.commit()
                return {"started": False, "reason": reasons[0]}
            self._event(db, "auto_execution_started", setup, symbol)
            db.commit()

        try:
            with self.session_factory() as db:
                _, execution_settings = load_stored_settings(db, self.settings)
            client = self.client_factory(execution_settings)
            price = Decimal((await client.ticker_price(symbol.symbol))["price"])
            info = (await client.exchange_info(symbol.symbol))["symbols"][0]
            account = await client.account()
            with self.session_factory() as db:
                setup = db.scalar(
                    select(TradeSetup)
                    .where(TradeSetup.id == setup_id)
                    .with_for_update()
                )
                symbol = db.get(Symbol, setup.symbol_id)
                existing = db.scalar(
                    select(ExecutionOrder).where(
                        ExecutionOrder.trade_setup_id == setup.id
                    )
                )
                if existing:
                    return {
                        "started": False,
                        "reason": "duplicate_setup_window",
                        "order_id": existing.id,
                    }
                reasons = self._preflight_reasons(db, setup, symbol, manual_approved)
                decision = ExecutionRiskEngine(execution_settings).evaluate(
                    db,
                    setup,
                    symbol,
                    account,
                    price,
                    info,
                    kill_switch=bool("kill_switch_enabled" in reasons),
                )
                reasons.extend(x for x in decision.reasons if x not in reasons)
                if reasons:
                    self._event(
                        db,
                        "auto_execution_skipped",
                        setup,
                        symbol,
                        reason=reasons[0],
                        metadata={"reasons": reasons},
                    )
                    db.commit()
                    return {"started": False, "reason": reasons[0]}
                cid = client_order_id("testnet", setup.id)
                order = ExecutionOrder(
                    environment="testnet",
                    symbol_id=symbol.id,
                    trade_setup_id=setup.id,
                    client_order_id=cid,
                    setup_fingerprint=setup_fingerprint(symbol.symbol, setup),
                    side="BUY",
                    order_type="MARKET",
                    requested_quantity=decision.adjusted_quantity,
                    status="created",
                    execution_state="created",
                )
                db.add(order)
                try:
                    db.flush()
                except IntegrityError:
                    db.rollback()
                    return {"started": False, "reason": "duplicate_setup_window"}
                self._event(db, "execution_order_created", setup, symbol, order=order)
                db.commit()
                order_id = order.id

            with self.session_factory() as db:
                order = db.get(ExecutionOrder, order_id)
                setup = db.get(TradeSetup, order.trade_setup_id)
                symbol = db.get(Symbol, order.symbol_id)
                order.status = "submitting"
                order.execution_state = "submitting"
                order.submitted_at = datetime.now(timezone.utc)
                self._event(
                    db, "exchange_submission_started", setup, symbol, order=order
                )
                db.commit()
                _, _, quantity_step = quantity_limits(info)
                quantity = serialize_quantity(
                    Decimal(order.requested_quantity), quantity_step
                )
                params = {
                    "symbol": symbol.symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": quantity,
                    "newClientOrderId": order.client_order_id,
                }

            await client.test_order(params)
            response = await client.place_order(params)
            with self.session_factory() as db:
                order = db.get(ExecutionOrder, order_id)
                setup = db.get(TradeSetup, order.trade_setup_id)
                symbol = db.get(Symbol, order.symbol_id)
                order.exchange_order_id = str(response.get("orderId"))
                order.status = response.get("status", "NEW")
                order.execution_state = (
                    "filled" if order.status == "FILLED" else "acknowledged"
                )
                order.acknowledged_at = datetime.now(timezone.utc)
                order.raw_status_json = response
                order.executed_quantity = Decimal(response.get("executedQty", "0"))
                if order.status == "FILLED":
                    order.filled_at = order.acknowledged_at
                setup.status = (
                    "executed" if order.status == "FILLED" else "acknowledged"
                )
                self._event(
                    db,
                    "exchange_submission_acknowledged",
                    setup,
                    symbol,
                    order=order,
                    metadata={"status": order.status},
                )
                if order.status == "FILLED":
                    self._event(
                        db,
                        "execution_filled",
                        setup,
                        symbol,
                        order=order,
                        metadata={"status": order.status, "source": "order_response"},
                    )
                db.commit()
                return {"started": True, "order_id": order.id, "status": order.status}
        except BinanceError as exc:
            return await self._handle_binance_error(setup_id, exc, client)
        except Exception as exc:
            with self.session_factory() as db:
                setup = db.get(TradeSetup, setup_id)
                symbol = db.get(Symbol, setup.symbol_id) if setup else None
                if setup and symbol:
                    order = db.scalar(
                        select(ExecutionOrder).where(
                            ExecutionOrder.trade_setup_id == setup_id
                        )
                    )
                    if order and order.execution_state in {"created", "submitting"}:
                        order.status = "execution_failed"
                        order.execution_state = "failed"
                        order.rejection_reason = str(exc) or type(exc).__name__
                    self._event(
                        db,
                        "execution_failed",
                        setup,
                        symbol,
                        order=order,
                        reason=str(exc) or type(exc).__name__,
                        severity="ERROR",
                    )
                    db.commit()
            return {"started": False, "reason": "execution_failed"}
        finally:
            if client:
                await client.close()

    async def _handle_binance_error(self, setup_id, exc, client):
        with self.session_factory() as db:
            order = db.scalar(
                select(ExecutionOrder).where(ExecutionOrder.trade_setup_id == setup_id)
            )
            setup = db.get(TradeSetup, setup_id)
            symbol = db.get(Symbol, setup.symbol_id)
            if not order:
                self._event(
                    db,
                    "execution_failed",
                    setup,
                    symbol,
                    reason=str(exc),
                    severity="ERROR",
                )
                db.commit()
                return {"started": False, "reason": str(exc)}
            if exc.unknown:
                order.status = "execution_unknown"
                order.execution_state = "unknown"
                order.rejection_reason = str(exc)
                db.commit()
                try:
                    found = await client.get_order(symbol.symbol, order.client_order_id)
                    order.raw_status_json = found
                    order.exchange_order_id = str(found.get("orderId"))
                    order.status = found.get("status", "UNKNOWN")
                    order.execution_state = "reconciled"
                    self._event(
                        db,
                        "exchange_submission_acknowledged",
                        setup,
                        symbol,
                        order=order,
                        metadata={"reconciled": True},
                    )
                    db.commit()
                    return {
                        "started": True,
                        "order_id": order.id,
                        "status": order.status,
                    }
                except BinanceError:
                    self._event(
                        db,
                        "execution_failed",
                        setup,
                        symbol,
                        order=order,
                        reason="execution_state_unknown",
                        severity="ERROR",
                    )
                    db.commit()
                    return {
                        "started": True,
                        "order_id": order.id,
                        "reason": "execution_state_unknown",
                    }
            order.status = "exchange_rejected"
            order.execution_state = "rejected"
            order.rejection_reason = str(exc)
            self._event(
                db,
                "exchange_submission_rejected",
                setup,
                symbol,
                order=order,
                reason=str(exc),
                severity="WARNING",
            )
            db.commit()
            return {"started": True, "order_id": order.id, "reason": str(exc)}


automatic_testnet_executor = AutomaticTestnetExecutor()
