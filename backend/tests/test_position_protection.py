from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.execution.protection import SpotProtectionService
from app.models import (
    BotRuntimeState,
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
    guard_response = None
    guard_rejection = None
    guard_submitted = None
    cancelled_order_lists = None
    cancelled_orders = None
    order_list_status = None
    order_details = None

    def __init__(self, settings):
        pass

    async def exchange_info(self, symbol):
        return {
            "symbols": [
                {
                    "orderTypes": [
                        "LIMIT",
                        "MARKET",
                        "STOP_LOSS",
                        "STOP_LOSS_LIMIT",
                        "LIMIT_MAKER",
                    ],
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
                    ],
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

    async def place_order(self, params):
        type(self).guard_submitted = dict(params)
        if self.guard_rejection:
            raise self.guard_rejection
        return self.guard_response or {
            "orderId": 950,
            "clientOrderId": params["newClientOrderId"],
            "status": "NEW",
        }

    async def cancel_order_list(self, symbol, order_list_id):
        type(self).cancelled_order_lists.append((symbol, order_list_id))
        return {}

    async def cancel_order(self, symbol, client_order_id):
        type(self).cancelled_orders.append((symbol, client_order_id))
        return {}

    async def get_order_list(self, order_list_id):
        if self.order_list_status is not None:
            return self.order_list_status
        return {"listOrderStatus": "EXECUTING"}

    async def get_order(self, symbol, client_order_id):
        details = self.order_details or {}
        return details.get(client_order_id, {"status": "NEW"})

    async def close(self):
        pass


def seed(
    session_factory,
    *,
    stop=Decimal("950"),
    tp1=Decimal("1100"),
    tp2=Decimal("1150"),
    tp3=Decimal("1200"),
    direction="long",
    quantity=Decimal("0.015779"),
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
            take_profit_2=tp2,
            take_profit_3=tp3,
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
            base_quantity=quantity,
            remaining_quantity=quantity,
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
    ProtectionClient.guard_response = None
    ProtectionClient.guard_rejection = None
    ProtectionClient.guard_submitted = None
    ProtectionClient.cancelled_order_lists = []
    ProtectionClient.cancelled_orders = []
    ProtectionClient.order_list_status = None
    ProtectionClient.order_details = None
    return SpotProtectionService(session_factory, ProtectionClient)


@pytest.mark.asyncio
async def test_small_position_collapses_to_single_full_quantity_bracket_with_stage_suffix(
    session_factory, monkeypatch
):
    position_id = seed(session_factory)
    result = await service(session_factory, monkeypatch).establish(position_id)
    assert result["protected"], result
    assert ProtectionClient.submitted == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "quantity": "0.01577",
        "listClientOrderId": f"ws-test-{position_id}-0-protect",
        "aboveType": "LIMIT_MAKER",
        "abovePrice": "1100",
        "aboveClientOrderId": f"ws-test-{position_id}-0-tp1",
        "belowType": "STOP_LOSS",
        "belowStopPrice": "950",
        "belowClientOrderId": f"ws-test-{position_id}-0-sl",
        "newOrderRespType": "FULL",
    }
    assert ProtectionClient.guard_submitted is None
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
        assert protection.role == "bracket"
        assert protection.stage == 0


@pytest.mark.asyncio
async def test_missing_target_fails_critically_without_submission(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, tp1=None, tp2=None, tp3=None)
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
            {"clientOrderId": f"ws-test-{position_id}-0-tp1"},
            {"clientOrderId": f"ws-test-{position_id}-0-sl"},
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


@pytest.mark.asyncio
async def test_large_position_scales_bracket_to_30_percent_and_guards_remainder(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, quantity=Decimal("10"))
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("10")
    result = await protection_service.establish(position_id)
    assert result["protected"], result
    assert ProtectionClient.submitted["quantity"] == "3"
    assert ProtectionClient.submitted["listClientOrderId"] == f"ws-test-{position_id}-0-protect"
    assert ProtectionClient.guard_submitted is not None
    assert ProtectionClient.guard_submitted["quantity"] == "7"
    assert ProtectionClient.guard_submitted["stopPrice"] == "950"
    assert ProtectionClient.guard_submitted["side"] == "SELL"
    assert ProtectionClient.guard_submitted["type"] == "STOP_LOSS"
    assert ProtectionClient.guard_submitted["newClientOrderId"] == f"ws-test-{position_id}-0-guard"
    with session_factory() as db:
        rows = list(db.scalars(select(ProtectiveOrder)))
        assert len(rows) == 2
        bracket = next(r for r in rows if r.role == "bracket")
        guard = next(r for r in rows if r.role == "guard_stop")
        assert bracket.quantity == Decimal("3")
        assert guard.quantity == Decimal("7")
        assert guard.stop_price == Decimal("950")
        assert guard.take_profit_price is None
        assert guard.take_profit_client_order_id is None
        assert guard.status == "protected"


@pytest.mark.asyncio
async def test_guard_placement_failure_unwinds_bracket_and_leaves_unprotected(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, quantity=Decimal("10"))
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("10")
    ProtectionClient.guard_rejection = RuntimeError("guard rejected")
    result = await protection_service.establish(position_id)
    assert not result["protected"]
    assert result["reason"] == "guard_order_failed"
    assert ProtectionClient.cancelled_order_lists == [("BTCUSDT", "901")]
    with session_factory() as db:
        position = db.get(LivePosition, position_id)
        assert position.protection_status == "unprotected"
        rows = list(db.scalars(select(ProtectiveOrder)))
        assert len(rows) == 1
        assert rows[0].role == "bracket"
        assert rows[0].status == "protection_failed"


@pytest.mark.asyncio
async def test_advance_stage_moves_stop_to_breakeven_and_places_next_bracket(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, quantity=Decimal("10"))
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("10")
    await protection_service.establish(position_id)

    with session_factory.begin() as db:
        position = db.get(LivePosition, position_id)
        position.remaining_quantity = Decimal("7")
        position.status = "partially_closed"

    ProtectionClient.submitted = None
    ProtectionClient.guard_submitted = None
    result = await protection_service.advance_stage(position_id)
    assert result["advanced"] is True
    assert result["moved_to_breakeven"] is True

    with session_factory() as db:
        position = db.get(LivePosition, position_id)
        assert position.stop_loss == Decimal("1000")
        assert position.breakeven_moved_at is not None
        assert position.tp1_filled_at is not None
        assert position.protection_stage == 1
        bracket_stage0 = db.scalar(
            select(ProtectiveOrder).where(
                ProtectiveOrder.stage == 0, ProtectiveOrder.role == "bracket"
            )
        )
        assert bracket_stage0.status == "closed"

    assert ProtectionClient.cancelled_orders == [
        ("BTCUSDT", f"ws-test-{position_id}-0-guard")
    ]
    assert ProtectionClient.submitted is not None
    assert ProtectionClient.submitted["aboveClientOrderId"] == f"ws-test-{position_id}-1-tp2"


@pytest.mark.asyncio
async def test_advance_stage_does_not_move_breakeven_again_on_later_stage(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, quantity=Decimal("10"))
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("10")
    await protection_service.establish(position_id)

    with session_factory.begin() as db:
        position = db.get(LivePosition, position_id)
        position.remaining_quantity = Decimal("7")
        position.status = "partially_closed"
    await protection_service.advance_stage(position_id)

    with session_factory() as db:
        stop_after_first = db.get(LivePosition, position_id).stop_loss

    with session_factory.begin() as db:
        position = db.get(LivePosition, position_id)
        position.remaining_quantity = Decimal("3")
        position.status = "partially_closed"
    result = await protection_service.advance_stage(position_id)

    assert result["moved_to_breakeven"] is False
    with session_factory() as db:
        position = db.get(LivePosition, position_id)
        assert position.stop_loss == stop_after_first == Decimal("1000")
        assert position.protection_stage == 2
        assert position.tp2_filled_at is not None


@pytest.mark.asyncio
async def test_reconcile_detects_filled_take_profit_leg_and_advances_stage(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, quantity=Decimal("10"))
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("10")
    await protection_service.establish(position_id)

    with session_factory.begin() as db:
        position = db.get(LivePosition, position_id)
        position.remaining_quantity = Decimal("7")
        position.status = "partially_closed"

    ProtectionClient.order_list_status = {
        "listOrderStatus": "ALL_DONE",
        "orderListId": 901,
        "symbol": "BTCUSDT",
        "orders": [
            {"orderId": 902, "clientOrderId": f"ws-test-{position_id}-0-tp1"},
            {"orderId": 903, "clientOrderId": f"ws-test-{position_id}-0-sl"},
        ],
    }
    ProtectionClient.order_details = {
        f"ws-test-{position_id}-0-tp1": {"status": "FILLED"},
        f"ws-test-{position_id}-0-sl": {"status": "CANCELED"},
    }

    result = await protection_service.reconcile(position_id)
    assert result["reconciled"] is True
    assert result.get("advanced", {}).get("advanced") is True

    with session_factory() as db:
        position = db.get(LivePosition, position_id)
        assert position.protection_stage == 1
        assert position.stop_loss == Decimal("1000")
        bracket = db.scalar(
            select(ProtectiveOrder).where(
                ProtectiveOrder.stage == 0, ProtectiveOrder.role == "bracket"
            )
        )
        assert bracket.status == "closed"


@pytest.mark.asyncio
async def test_establish_uses_custom_tp_fractions_from_risk_config_when_valid(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, quantity=Decimal("10"))
    with session_factory.begin() as db:
        db.add(
            BotRuntimeState(
                risk_config_json={"tp1_pct": "50", "tp2_pct": "30", "tp3_pct": "20"}
            )
        )
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("10")
    result = await protection_service.establish(position_id)
    assert result["protected"], result
    assert ProtectionClient.submitted["quantity"] == "5"
    assert ProtectionClient.guard_submitted is not None
    assert ProtectionClient.guard_submitted["quantity"] == "5"
    with session_factory() as db:
        event = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.event_type == "tp_fraction_config_invalid"
            )
        )
        assert event is None


@pytest.mark.asyncio
async def test_establish_falls_back_to_paper_forward_fractions_when_percentages_invalid(
    session_factory, monkeypatch
):
    position_id = seed(session_factory, quantity=Decimal("10"))
    with session_factory.begin() as db:
        db.add(
            BotRuntimeState(
                risk_config_json={"tp1_pct": "50", "tp2_pct": "30", "tp3_pct": "30"}
            )
        )
    protection_service = service(session_factory, monkeypatch)
    ProtectionClient.available = Decimal("10")
    result = await protection_service.establish(position_id)
    assert result["protected"], result
    # Falls back to paper_forward.TP_FRACTIONS (30/40/30) since 50+30+30 != 100.
    assert ProtectionClient.submitted["quantity"] == "3"
    assert ProtectionClient.guard_submitted is not None
    assert ProtectionClient.guard_submitted["quantity"] == "7"
    with session_factory() as db:
        event = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.event_type == "tp_fraction_config_invalid"
            )
        )
        assert event is not None
        assert event.severity == "WARNING"
        assert event.metadata_json["reason"] == "tp_fraction_config_not_100_pct"
