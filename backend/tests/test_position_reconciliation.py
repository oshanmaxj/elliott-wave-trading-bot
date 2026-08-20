from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.execution.reconciliation import PositionReconciliationService, position_mark
from app.models import LivePosition, Symbol, TradeSetup


def test_long_position_mark_price_profit_loss_and_flat():
    position = SimpleNamespace(average_entry=Decimal("100"), remaining_quantity=Decimal("2"))
    assert position_mark(position, Decimal("110"))["unrealized_pnl"] == Decimal("20")
    assert position_mark(position, Decimal("90"))["unrealized_pnl"] == Decimal("-20")
    flat = position_mark(position, Decimal("100"))
    assert flat["unrealized_pnl"] == 0
    assert flat["market_value"] == flat["cost_basis"] == Decimal("200")
    assert position_mark(position, None)["unrealized_pnl"] is None


def seed_position(session_factory):
    now = datetime.now(timezone.utc)
    with session_factory.begin() as db:
        symbol = Symbol(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", is_active=True)
        db.add(symbol)
        db.flush()
        setup = TradeSetup(symbol_id=symbol.id, direction="bullish", strategy="bullish_continuation",
            status="executed", higher_timeframe="5m", setup_timeframe="1m", entry_timeframe="1m",
            structure_event_id=1, entry_min=Decimal("99"), entry_max=Decimal("101"),
            preferred_entry=Decimal("100"), stop_loss=Decimal("95"), invalidation_price=Decimal("95"),
            take_profit_1=Decimal("110"), risk_reward_1=Decimal("2"), confidence_score=Decimal("90"),
            expires_at=now+timedelta(hours=1), detected_at=now)
        db.add(setup)
        db.flush()
        position = LivePosition(environment="testnet", symbol_id=symbol.id,
            originating_trade_setup_id=setup.id, direction="long", status="open",
            base_quantity=Decimal("1"), remaining_quantity=Decimal("1"), average_entry=Decimal("100"),
            stop_loss=Decimal("0"), protection_status="unprotected", opened_at=now)
        db.add(position)
        db.flush()
        return position.id


class FakeClient:
    balance = Decimal("0")
    def __init__(self, settings): pass
    async def account(self):
        return {"balances": [{"asset": "BTC", "free": str(self.balance), "locked": "0"}]}
    async def open_orders(self, symbol): return []
    async def trades(self, symbol): return []
    async def close(self): pass


@pytest.mark.asyncio
async def test_exchange_balance_proves_external_close_and_is_idempotent(session_factory):
    position_id = seed_position(session_factory)
    FakeClient.balance = Decimal("0")
    service = PositionReconciliationService(session_factory, FakeClient)
    service._client_settings = lambda: SimpleNamespace(binance_environment="testnet")
    first = await service.reconcile_all()
    second = await service.reconcile_all()
    assert first["positions"][0]["status"] == "closed"
    assert second["checked"] == 0
    with session_factory() as db:
        row = db.get(LivePosition, position_id)
        assert row.status == "closed" and row.remaining_quantity == 0
        assert row.last_reconciled_at is not None


@pytest.mark.asyncio
async def test_real_balance_keeps_position_and_repairs_remaining_quantity(session_factory):
    position_id = seed_position(session_factory)
    FakeClient.balance = Decimal("0.4")
    service = PositionReconciliationService(session_factory, FakeClient)
    service._client_settings = lambda: SimpleNamespace(binance_environment="testnet")
    await service.reconcile_all()
    with session_factory() as db:
        row = db.get(LivePosition, position_id)
        assert row.status == "partially_closed"
        assert row.remaining_quantity == Decimal("0.4")
        assert row.protection_status == "unprotected"
        assert row.stop_loss == Decimal("95")
        assert row.take_profit_1 == Decimal("110")


@pytest.mark.asyncio
async def test_reconciliation_preserves_existing_protection_levels(session_factory):
    position_id = seed_position(session_factory)
    with session_factory.begin() as db:
        row = db.get(LivePosition, position_id)
        row.stop_loss = Decimal("94")
        row.take_profit_1 = Decimal("111")
        row.take_profit_2 = Decimal("120")

    FakeClient.balance = Decimal("1")
    service = PositionReconciliationService(session_factory, FakeClient)
    service._client_settings = lambda: SimpleNamespace(binance_environment="testnet")
    await service.reconcile_all()

    with session_factory() as db:
        row = db.get(LivePosition, position_id)
        assert (row.stop_loss, row.take_profit_1, row.take_profit_2) == (
            Decimal("94"), Decimal("111"), Decimal("120")
        )
