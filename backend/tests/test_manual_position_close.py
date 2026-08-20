from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.execution.binance import BinanceError
from app.execution.position_close import ManualCloseError, ManualPositionCloseService
from app.models import (
    BotRuntimeState,
    ExecutionEvent,
    ExecutionOrder,
    LivePosition,
    ProtectiveOrder,
    Symbol,
    TradeSetup,
)


class CloseClient:
    free = Decimal("0.2")
    response = None
    rejection = None
    canceled = []
    submitted = []
    cancellation_error = None

    def __init__(self, settings): pass
    async def exchange_info(self, symbol):
        return {"symbols": [{"status": "TRADING", "orderTypes": ["MARKET"],
            "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001",
                         "maxQty": "100", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "10"}]}]}
    async def account(self):
        return {"balances": [{"asset": "BTC", "free": str(self.free), "locked": "0"}]}
    async def ticker_price(self, symbol): return {"price": "100"}
    async def cancel_order_list(self, symbol, order_list_id):
        if self.cancellation_error:
            raise self.cancellation_error
        self.canceled.append((symbol, order_list_id))
        return {"listOrderStatus": "ALL_DONE"}
    async def place_order(self, params):
        self.submitted.append(params)
        if self.rejection:
            raise self.rejection
        return self.response or {"orderId": 88, "status": "FILLED",
            "executedQty": params["quantity"],
            "cummulativeQuoteQty": str(Decimal(params["quantity"]) * 100),
            "fills": [{"tradeId": 9, "price": "100", "qty": params["quantity"],
                       "commission": "0.01", "commissionAsset": "USDT"}]}
    async def get_order(self, symbol, cid): return self.response
    async def close(self): pass


def seed(session_factory, *, position_status="open", runtime_status="running",
         kill=False, protection=False):
    now = datetime.now(timezone.utc)
    with session_factory.begin() as db:
        symbol = Symbol(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
                        market_type="spot")
        db.add(symbol)
        db.flush()
        setup = TradeSetup(symbol_id=symbol.id, direction="bullish",
            strategy="bullish_continuation", status="executed", higher_timeframe="5m",
            setup_timeframe="1m", entry_timeframe="1m", structure_event_id=1,
            entry_min=Decimal("89"), entry_max=Decimal("91"),
            preferred_entry=Decimal("90"), stop_loss=Decimal("80"),
            invalidation_price=Decimal("80"), take_profit_1=Decimal("110"),
            risk_reward_1=Decimal("2"), confidence_score=Decimal("90"),
            expires_at=now + timedelta(hours=1), detected_at=now)
        db.add(setup)
        db.flush()
        position = LivePosition(environment="testnet", symbol_id=symbol.id,
            originating_trade_setup_id=setup.id, direction="long", status=position_status,
            base_quantity=Decimal("0.2"), remaining_quantity=Decimal("0.2"),
            average_entry=Decimal("90"), stop_loss=Decimal("80"),
            take_profit_1=Decimal("110"), protection_status="protected" if protection else "unprotected",
            opened_at=now)
        db.add(position)
        db.flush()
        if protection:
            db.add(ProtectiveOrder(live_position_id=position.id, environment="testnet",
                symbol_id=symbol.id, order_list_id="77", list_client_order_id="protect-1",
                stop_client_order_id="sl-1", take_profit_client_order_id="tp-1",
                quantity=Decimal("0.2"), stop_price=Decimal("80"),
                take_profit_price=Decimal("110"), status="protected"))
        db.add(BotRuntimeState(status=runtime_status, automatic_trading_enabled=True,
            pause_new_entries=runtime_status != "running", kill_switch_enabled=kill))
        return position.id


def service(session_factory, monkeypatch):
    monkeypatch.setattr("app.execution.position_close.load_stored_settings",
                        lambda db, settings: (None, settings))
    CloseClient.free, CloseClient.response, CloseClient.rejection = Decimal("0.2"), None, None
    CloseClient.cancellation_error = None
    CloseClient.canceled, CloseClient.submitted = [], []
    settings = SimpleNamespace(binance_environment="testnet")
    return ManualPositionCloseService(session_factory, CloseClient, settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_status,kill", [("running", False), ("stopped", False),
                                                   ("running", True)])
async def test_full_close_allowed_regardless_of_entry_controls(
    runtime_status, kill, session_factory, monkeypatch
):
    position_id = seed(session_factory, runtime_status=runtime_status, kill=kill)
    result = await service(session_factory, monkeypatch).close_position(position_id, "admin")
    assert result["position_status"] == "closed"
    with session_factory() as db:
        position = db.get(LivePosition, position_id)
        assert position.remaining_quantity == 0
        assert position.exit_reason == "manual_close"
        assert position.exit_price == Decimal("100")
        assert position.realized_pnl == Decimal("1.99")
        assert db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.event_type == "manual_position_closed"))


@pytest.mark.asyncio
async def test_partial_fill_keeps_position_open_for_another_attempt(session_factory, monkeypatch):
    position_id = seed(session_factory)
    close = service(session_factory, monkeypatch)
    CloseClient.response = {"orderId": 88, "status": "PARTIALLY_FILLED",
        "executedQty": "0.1", "cummulativeQuoteQty": "10", "fills": []}
    result = await close.close_position(position_id, "admin")
    assert result["position_status"] == "partially_closed"
    assert result["remaining_quantity"] == "0.100000000000"
    CloseClient.response = None
    await close.close_position(position_id, "admin")
    with session_factory() as db:
        assert db.get(LivePosition, position_id).status == "closed"
        assert len(list(db.scalars(select(ExecutionOrder)))) == 2


@pytest.mark.asyncio
async def test_existing_protection_is_canceled_before_close(session_factory, monkeypatch):
    position_id = seed(session_factory, protection=True)
    await service(session_factory, monkeypatch).close_position(position_id, "admin")
    assert CloseClient.canceled == [("BTCUSDT", "77")]
    with session_factory() as db:
        assert db.scalar(select(ProtectiveOrder)).status == "closed"


@pytest.mark.asyncio
async def test_protection_cancellation_failure_aborts_sell(session_factory, monkeypatch):
    position_id = seed(session_factory, protection=True)
    close = service(session_factory, monkeypatch)
    CloseClient.cancellation_error = BinanceError("cancel rejected")
    with pytest.raises(ManualCloseError, match="protection_cancellation_failed"):
        await close.close_position(position_id, "admin")
    assert CloseClient.submitted == []
    with session_factory() as db:
        assert db.get(LivePosition, position_id).status == "open"


@pytest.mark.asyncio
async def test_wallet_position_mismatch_never_sells_above_free_balance(
    session_factory, monkeypatch
):
    position_id = seed(session_factory)
    close = service(session_factory, monkeypatch)
    CloseClient.free = Decimal("0.1")
    result = await close.close_position(position_id, "admin")
    assert CloseClient.submitted[0]["quantity"] == "0.1"
    assert result["position_status"] == "partially_closed"
    assert result["remaining_quantity"] == "0.100000000000"


@pytest.mark.asyncio
async def test_already_closed_duplicate_rejection_and_insufficient_quantity(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, position_status="closed")
    with pytest.raises(ManualCloseError, match="position_already_closed"):
        await service(session_factory, monkeypatch).close_position(position_id, "admin")


@pytest.mark.asyncio
async def test_duplicate_active_request_does_not_submit(session_factory, monkeypatch):
    position_id = seed(session_factory)
    with session_factory.begin() as db:
        position = db.get(LivePosition, position_id)
        db.add(ExecutionOrder(environment="testnet", symbol_id=position.symbol_id,
            trade_setup_id=position.originating_trade_setup_id,
            client_order_id=f"ws-test-pos-{position_id}-close-1", side="SELL",
            order_type="MARKET", requested_quantity=Decimal("0.2"),
            status="submitting", execution_state="submitting"))
    result = await service(session_factory, monkeypatch).close_position(position_id, "admin")
    assert result["duplicate"] is True
    assert CloseClient.submitted == []


@pytest.mark.asyncio
async def test_rejection_and_insufficient_balance_remain_open(session_factory, monkeypatch):
    position_id = seed(session_factory)
    close = service(session_factory, monkeypatch)
    CloseClient.rejection = BinanceError("rejected")
    with pytest.raises(ManualCloseError, match="rejected"):
        await close.close_position(position_id, "admin")
    with session_factory() as db:
        assert db.get(LivePosition, position_id).status == "open"
        assert db.scalar(select(ExecutionOrder)).execution_state == "rejected"

    with session_factory.begin() as db:
        db.query(ExecutionOrder).delete()
    CloseClient.rejection, CloseClient.free = None, Decimal("0")
    with pytest.raises(ManualCloseError, match="insufficient_reconciled_quantity"):
        await close.close_position(position_id, "admin")
