from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.execution.protection import SpotProtectionService
from app.models import (
    ExecutionEvent,
    LivePosition,
    ProtectiveOrder,
    Symbol,
    TradeSetup,
)


class ProtectionClient:
    response = None
    rejection = None
    submitted = None
    available = Decimal("0.015779")

    def __init__(self, settings):
        pass

    async def exchange_info(self, symbol):
        return {
            "symbols": [
                {
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.01",
                            "maxPrice": "1000000",
                            "tickSize": "0.10",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.00001",
                            "maxQty": "9000",
                            "stepSize": "0.00001",
                        },
                        {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
                    ]
                }
            ]
        }

    async def account(self):
        return {"balances": [{"asset": "BTC", "free": str(self.available), "locked": "0"}]}

    async def place_oco_order(self, params):
        type(self).submitted = dict(params)
        if self.rejection:
            raise self.rejection
        return self.response or {
            "orderListId": 901,
            "listOrderStatus": "EXECUTING",
            "orders": [
                {"orderId": 902, "clientOrderId": params["aboveClientOrderId"]},
                {"orderId": 903, "clientOrderId": params["belowClientOrderId"]},
            ],
        }

    async def close(self):
        pass


def seed(
    session_factory, *, stop=Decimal("950"), tp1=Decimal("1100"), direction="long"
):
    now = datetime.now(timezone.utc)
    with session_factory.begin() as db:
        symbol = Symbol(
            exchange="binance",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            market_type="spot",
        )
        db.add(symbol)
        db.flush()
        setup = TradeSetup(
            symbol_id=symbol.id,
            direction="bullish" if direction == "long" else "bearish",
            strategy="bullish_continuation",
            status="executed",
            higher_timeframe="5m",
            setup_timeframe="1m",
            entry_timeframe="1m",
            structure_event_id=1,
            entry_min=Decimal("990"),
            entry_max=Decimal("1010"),
            preferred_entry=Decimal("1000"),
            stop_loss=stop,
            invalidation_price=stop,
            take_profit_1=tp1,
            take_profit_2=Decimal("1150"),
            take_profit_3=Decimal("1200"),
            risk_reward_1=Decimal("2"),
            risk_reward_2=Decimal("3"),
            risk_reward_3=Decimal("4"),
            confidence_score=Decimal("90"),
            expires_at=now + timedelta(hours=1),
            detected_at=now,
        )
        db.add(setup)
        db.flush()
        position = LivePosition(
            environment="testnet",
            symbol_id=symbol.id,
            originating_trade_setup_id=setup.id,
            direction=direction,
            status="open",
            base_quantity=Decimal("0.015779"),
            remaining_quantity=Decimal("0.015779"),
            average_entry=Decimal("1000"),
            stop_loss=stop or Decimal("0"),
            take_profit_1=tp1,
            take_profit_2=setup.take_profit_2,
            take_profit_3=setup.take_profit_3,
            protection_status="protection_pending",
            opened_at=now,
        )
        db.add(position)
        db.flush()
        return position.id


def service(session_factory, monkeypatch):
    monkeypatch.setattr(
        "app.execution.protection.load_stored_settings",
        lambda db, settings: (None, settings),
    )
    monkeypatch.setattr("app.execution.protection.log_event", lambda *a, **k: None)
    ProtectionClient.response = None
    ProtectionClient.rejection = None
    ProtectionClient.submitted = None
    ProtectionClient.available = Decimal("0.015779")
    return SpotProtectionService(session_factory, ProtectionClient)


@pytest.mark.asyncio
async def test_filled_position_retains_setup_geometry_and_gets_exchange_oco(
    session_factory, monkeypatch
):
    position_id = seed(session_factory)
    result = await service(session_factory, monkeypatch).establish(position_id)
    assert result["protected"], result
    assert ProtectionClient.submitted == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "quantity": "0.01577",
        "listClientOrderId": f"ws-test-{position_id}-protect",
        "aboveType": "LIMIT_MAKER",
        "abovePrice": "1100",
        "aboveClientOrderId": f"ws-test-{position_id}-tp1",
        "belowType": "STOP_LOSS",
        "belowStopPrice": "950",
        "belowClientOrderId": f"ws-test-{position_id}-sl",
        "newOrderRespType": "FULL",
    }
    with session_factory() as db:
        position = db.get(LivePosition, position_id)
        protection = db.scalar(select(ProtectiveOrder))
        assert position.protection_status == "protected"
        assert (
            position.stop_loss,
            position.take_profit_1,
            position.take_profit_2,
            position.take_profit_3,
        ) == (Decimal("950"), Decimal("1100"), Decimal("1150"), Decimal("1200"))
        assert protection.order_list_id == "901"
        assert protection.stop_exchange_order_id == "903"
        assert protection.take_profit_exchange_order_id == "902"


@pytest.mark.asyncio
async def test_missing_target_fails_critically_without_submission(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, tp1=None)
    result = await service(session_factory, monkeypatch).establish(position_id)
    assert result == {"protected": False, "reason": "missing_protective_target"}
    assert ProtectionClient.submitted is None
    with session_factory() as db:
        position = db.get(LivePosition, position_id)
        event = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.event_type == "protection_failed"
            )
        )
        assert position.protection_status == "unprotected"
        assert event.severity == "CRITICAL"


@pytest.mark.asyncio
async def test_non_executing_oco_is_not_reported_as_protected(
    session_factory, monkeypatch
):
    position_id = seed(session_factory)
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.response = {
        "orderListId": 901,
        "listOrderStatus": "REJECT",
        "orders": [],
    }
    result = await protection_service.establish(position_id)
    assert not result["protected"]
    with session_factory() as db:
        assert db.get(LivePosition, position_id).protection_status == "unprotected"
        assert db.scalar(select(ProtectiveOrder)).status == "protection_failed"


@pytest.mark.asyncio
async def test_short_position_uses_inverse_acknowledged_oco_geometry(
    session_factory, monkeypatch
):
    position_id = seed(
        session_factory, stop=Decimal("1050"), tp1=Decimal("900"), direction="short"
    )
    result = await service(session_factory, monkeypatch).establish(position_id)
    assert result["protected"], result
    assert ProtectionClient.submitted["side"] == "BUY"
    assert ProtectionClient.submitted["aboveType"] == "STOP_LOSS"
    assert ProtectionClient.submitted["aboveStopPrice"] == "1050"
    assert ProtectionClient.submitted["belowType"] == "LIMIT_MAKER"
    assert ProtectionClient.submitted["belowPrice"] == "900"


@pytest.mark.asyncio
async def test_acknowledgement_without_leg_ids_remains_unprotected(
    session_factory, monkeypatch
):
    position_id = seed(session_factory)
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.response = {
        "orderListId": 901,
        "listOrderStatus": "EXECUTING",
        "orders": [
            {"clientOrderId": f"ws-test-{position_id}-tp1"},
            {"clientOrderId": f"ws-test-{position_id}-sl"},
        ],
    }
    result = await protection_service.establish(position_id)
    assert not result["protected"]
    with session_factory() as db:
        assert db.get(LivePosition, position_id).protection_status == "unprotected"


@pytest.mark.asyncio
async def test_rejection_event_contains_safe_incident_context(
    session_factory, monkeypatch
):
    position_id = seed(session_factory)
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.rejection = RuntimeError("testnet rejected protection")
    result = await protection_service.establish(position_id)
    assert not result["protected"]
    with session_factory() as db:
        event = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.event_type == "protection_failed"
            )
        )
        assert event.severity == "CRITICAL"
        assert event.metadata_json["setup_id"]
        assert event.metadata_json["position_id"] == position_id
        assert event.metadata_json["binance_error"] == "testnet rejected protection"
        assert event.metadata_json["attempted_parameters"]["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_sellable_balance_caps_and_floors_protection_quantity(session_factory, monkeypatch):
    position_id = seed(session_factory)
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("0.012349")
    result = await protection_service.establish(position_id)
    assert result["protected"]
    assert ProtectionClient.submitted["quantity"] == "0.01234"


@pytest.mark.asyncio
async def test_insufficient_sellable_balance_fails_without_submission(session_factory, monkeypatch):
    position_id = seed(session_factory)
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("0")
    result = await protection_service.establish(position_id)
    assert not result["protected"]
    assert result["reason"] == "invalid_protective_quantity"
    assert ProtectionClient.submitted is None
