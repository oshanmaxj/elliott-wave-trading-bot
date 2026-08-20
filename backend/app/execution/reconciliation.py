import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import log_event
from app.database.session import SessionLocal
from app.execution.binance import BinanceSpotClient
from app.execution.credentials import load_stored_settings
from app.execution.positions import restore_setup_protection_levels
from app.models import ExecutionEvent, LivePosition, ProtectiveOrder, Symbol, TradeSetup

ACTIVE_POSITION_STATUSES = ("open", "partially_closed")


def active_positions_query():
    return select(LivePosition).where(LivePosition.status.in_(ACTIVE_POSITION_STATUSES))


def position_mark(position, current_price: Decimal | None) -> dict:
    quantity = Decimal(position.remaining_quantity)
    entry = Decimal(position.average_entry)
    if current_price is None:
        return {"current_price": None, "market_value": None, "cost_basis": entry * quantity,
                "unrealized_pnl": None, "unrealized_pnl_pct": None}
    market_value = current_price * quantity
    cost_basis = entry * quantity
    pnl = (current_price - entry) * quantity
    pct = (pnl / cost_basis * Decimal("100")) if cost_basis else None
    return {"current_price": current_price, "market_value": market_value,
            "cost_basis": cost_basis, "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pct}


class PositionReconciliationService:
    """Read-only exchange inspection followed by idempotent local state repair."""

    def __init__(self, session_factory=SessionLocal, client_factory=BinanceSpotClient,
                 settings=None):
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.settings = settings or get_settings()

    def _client_settings(self):
        with self.session_factory() as db:
            _, configured = load_stored_settings(db, self.settings)
        if configured.binance_environment != "testnet":
            raise RuntimeError("Position reconciliation is restricted to Binance Spot Testnet")
        return configured

    async def marks(self) -> dict[int, dict]:
        with self.session_factory() as db:
            rows = [(p.id, db.get(Symbol, p.symbol_id).symbol, p) for p in db.scalars(
                active_positions_query())]
        if not rows:
            return {}
        client = self.client_factory(self._client_settings())
        try:
            prices = {}
            for _, symbol, _ in rows:
                if symbol not in prices:
                    try:
                        prices[symbol] = Decimal((await client.ticker_price(symbol))["price"])
                    except Exception:
                        prices[symbol] = None
            return {position_id: position_mark(position, prices[symbol])
                    for position_id, symbol, position in rows}
        finally:
            await client.close()

    async def reconcile_all(self) -> dict:
        configured = self._client_settings()
        client = self.client_factory(configured)
        now = datetime.now(timezone.utc)
        results = []
        try:
            account = await client.account()
            balances = {row["asset"]: Decimal(row["free"]) + Decimal(row["locked"])
                        for row in account.get("balances", [])}
            with self.session_factory() as db:
                position_ids = list(db.scalars(select(LivePosition.id).where(
                    LivePosition.environment == "testnet",
                    LivePosition.status.in_(ACTIVE_POSITION_STATUSES))))
            for position_id in position_ids:
                with self.session_factory() as db:
                    position = db.get(LivePosition, position_id)
                    symbol = db.get(Symbol, position.symbol_id)
                    protection = db.scalar(select(ProtectiveOrder).where(
                        ProtectiveOrder.live_position_id == position.id
                    ).order_by(ProtectiveOrder.id.desc()))
                    setup = db.get(TradeSetup, position.originating_trade_setup_id)
                    protection_levels_restored = restore_setup_protection_levels(
                        position, setup
                    )
                    previous_quantity = Decimal(position.remaining_quantity)
                    owned = balances.get(symbol.base_asset, Decimal("0"))
                    # Never increase a bot position from unrelated account holdings.
                    remaining = min(previous_quantity, owned)
                    await client.open_orders(symbol.symbol)
                    await client.trades(symbol.symbol)  # verifies signed trade-history access
                    protective_open = False
                    if protection and protection.order_list_id:
                        try:
                            order_list = await client.get_order_list(protection.order_list_id)
                            listed = {str(order.get("orderId")) for order in order_list.get("orders", [])}
                            protective_open = (
                                order_list.get("listOrderStatus") == "EXECUTING"
                                and bool(protection.stop_exchange_order_id)
                                and bool(protection.take_profit_exchange_order_id)
                                and protection.stop_exchange_order_id in listed
                                and protection.take_profit_exchange_order_id in listed
                            )
                        except Exception:
                            protective_open = False
                    position.remaining_quantity = remaining
                    position.last_reconciled_at = now
                    if remaining == 0:
                        position.status = "closed"
                        position.protection_status = "closed"
                        position.closed_at = now
                    else:
                        position.status = "partially_closed" if remaining < position.base_quantity else "open"
                        position.protection_status = "protected" if protective_open else "unprotected"
                    db.add(ExecutionEvent(
                        severity="WARNING" if position.protection_status == "unprotected" else "INFO",
                        event_type="position_reconciled", exchange="binance", environment="testnet",
                        symbol_id=symbol.id, trade_setup_id=position.originating_trade_setup_id,
                        live_position_id=position.id, message="Live position reconciled from Binance account state",
                        metadata_json={"previous_quantity": str(previous_quantity),
                                       "remaining_quantity": str(remaining),
                                       "account_base_balance": str(owned),
                                       "protective_orders_open": protective_open,
                                       "protection_levels_restored": protection_levels_restored,
                                       "status": position.status}))
                    db.commit()
                    results.append({"position_id": position.id, "symbol": symbol.symbol,
                                    "previous_quantity": str(previous_quantity),
                                    "remaining_quantity": str(remaining), "status": position.status,
                                    "protection_status": position.protection_status})
            return {"checked": len(position_ids), "reconciled": len(results), "positions": results,
                    "reconciled_at": now}
        finally:
            await client.close()

    async def run_periodically(self, interval_seconds=60):
        while True:
            try:
                await self.reconcile_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event("ERROR", "execution", "position_reconciliation_failed",
                          str(exc) or type(exc).__name__,
                          {"exception_type": type(exc).__name__})
            await asyncio.sleep(interval_seconds)


position_reconciliation = PositionReconciliationService()
