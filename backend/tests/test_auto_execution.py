from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.execution.binance import BinanceError
from app.execution.orchestrator import AutomaticTestnetExecutor
from app.api.execution import queue
from app.models import (
    BotLog,
    BotRuntimeState,
    ExecutionEvent,
    ExecutionOrder,
    LivePosition,
    Symbol,
    TradeSetup,
)
from app.repositories.market import upsert_candle
from app.schemas.common import CandleData
from app.services.pipeline import process_closed_candle


def execution_settings(**updates):
    values = dict(
        binance_execution_enabled=True,
        binance_environment="testnet",
        execution_mode="automatic_testnet",
        execution_require_manual_approval=False,
        binance_api_key="test-key",
        binance_api_secret="test-secret",
        allowed_execution_symbols="BTCUSDT,ETHUSDT",
        allowed_execution_strategies="bullish_continuation",
        min_execution_confidence=Decimal("75"),
        max_risk_per_trade_pct=Decimal("0.25"),
        max_daily_loss_pct=Decimal("1"),
        max_symbol_exposure_pct=Decimal("10"),
        max_open_positions=1,
        allow_production_orders=False,
    )
    values.update(updates)
    return Settings(**values)


def seed(
    session_factory,
    *,
    direction="bullish",
    strategy="bullish_continuation",
    runtime_strategies=None,
    status="running",
    setup_status="ready",
    paused=False,
    automatic=True,
    enabled=True,
    kill=False,
    invalid_geometry=False,
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
        stop = Decimal("105") if invalid_geometry else Decimal("95")
        setup = TradeSetup(
            symbol_id=symbol.id,
            direction=direction,
            strategy=strategy,
            status=setup_status,
            higher_timeframe="5m",
            setup_timeframe="1m",
            entry_timeframe="1m",
            structure_event_id=1,
            entry_min=Decimal("99"),
            entry_max=Decimal("101"),
            preferred_entry=Decimal("100"),
            stop_loss=stop,
            invalidation_price=stop,
            take_profit_1=Decimal("110"),
            take_profit_2=Decimal("115"),
            take_profit_3=Decimal("120"),
            risk_reward_1=2,
            risk_reward_2=3,
            risk_reward_3=4,
            confidence_score=90,
            expires_at=now + timedelta(hours=1),
            detected_at=now,
        )
        db.add(setup)
        db.add(
            BotRuntimeState(
                status=status,
                environment="testnet",
                automatic_trading_enabled=automatic,
                manual_approval_required=False,
                pause_new_entries=paused,
                kill_switch_enabled=kill,
                enabled_symbols_json=["BTCUSDT"] if enabled else [],
                enabled_timeframes_json=["1m"] if enabled else [],
                enabled_strategies_json=(runtime_strategies or ["bos_continuation"])
                if enabled
                else [],
            )
        )
        db.flush()
        return setup.id


class Client:
    submissions = 0
    rejection = None
    quantity_filters = [
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0",
            "maxQty": "141.67845966",
            "stepSize": "0",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00001",
            "maxQty": "9000",
            "stepSize": "0.00001",
        },
    ]
    submitted_params = []
    response = {"orderId": 77, "status": "NEW", "executedQty": "0"}
    unexpected_error = None

    def __init__(self, settings):
        pass

    async def ticker_price(self, symbol):
        return {"price": "100"}

    async def exchange_info(self, symbol):
        return {
            "symbols": [
                {
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                    "orderTypes": ["MARKET"],
                    "filters": [
                        *self.quantity_filters,
                        {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
                    ],
                }
            ]
        }

    async def account(self):
        return {
            "balances": [
                {"asset": "USDT", "free": "10000"},
                {"asset": "BTC", "free": "0"},
            ]
        }

    async def test_order(self, params):
        type(self).submitted_params.append(("test", dict(params)))
        return {}

    async def place_order(self, params):
        type(self).submitted_params.append(("place", dict(params)))
        type(self).submissions += 1
        if self.rejection:
            raise self.rejection
        if self.unexpected_error:
            raise self.unexpected_error
        return dict(type(self).response)

    async def get_order(self, symbol, client_order_id):
        return {"orderId": 77, "status": "NEW", "executedQty": "0"}

    async def close(self):
        pass


def executor(session_factory, monkeypatch, settings=None):
    configured = settings or execution_settings()
    monkeypatch.setattr(
        "app.execution.orchestrator.load_stored_settings",
        lambda db, base: (None, configured),
    )
    monkeypatch.setattr(
        "app.execution.orchestrator.log_event", lambda *args, **kwargs: None
    )
    Client.submissions, Client.rejection, Client.submitted_params = 0, None, []
    Client.quantity_filters = [
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0",
            "maxQty": "141.67845966",
            "stepSize": "0",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00001",
            "maxQty": "9000",
            "stepSize": "0.00001",
        },
    ]
    Client.response = {"orderId": 77, "status": "NEW", "executedQty": "0"}
    Client.unexpected_error = None
    return AutomaticTestnetExecutor(session_factory, Client, configured)


@pytest.mark.asyncio
async def test_eligible_automatic_testnet_setup_submits_exactly_once(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    service = executor(session_factory, monkeypatch)
    first = await service.handoff(setup_id)
    second = await service.handoff(setup_id)
    assert first["started"], first
    assert second["reason"] == "duplicate_setup_window"
    assert Client.submissions == 1
    assert [params["quantity"] for _, params in Client.submitted_params] == ["5", "5"]
    with session_factory() as db:
        order = db.scalar(select(ExecutionOrder))
        assert order.exchange_order_id == "77" and order.status == "NEW"
        assert db.scalar(select(func.count(ExecutionOrder.id))) == 1


@pytest.mark.asyncio
async def test_unprotected_position_blocks_new_symbol_entry(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    with session_factory.begin() as db:
        setup = db.get(TradeSetup, setup_id)
        db.add(
            LivePosition(
                environment="testnet",
                symbol_id=setup.symbol_id,
                originating_trade_setup_id=setup.id,
                direction="long",
                status="open",
                base_quantity=Decimal(".1"),
                remaining_quantity=Decimal(".1"),
                average_entry=Decimal("100"),
                stop_loss=Decimal("95"),
                take_profit_1=Decimal("110"),
                protection_status="unprotected",
                opened_at=datetime.now(timezone.utc),
            )
        )
    result = await executor(session_factory, monkeypatch).handoff(setup_id)
    assert result["reason"] == "symbol_has_unprotected_position"
    assert Client.submissions == 0


@pytest.mark.asyncio
async def test_triggered_setup_passes_final_risk_engine_and_submits(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory, setup_status="triggered")
    result = await executor(session_factory, monkeypatch).handoff(setup_id)
    assert result["started"] and Client.submissions == 1


@pytest.mark.asyncio
async def test_expired_setup_is_blocked_before_binance_client_creation(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    with session_factory.begin() as db:
        db.get(TradeSetup, setup_id).expires_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
    result = await executor(session_factory, monkeypatch).handoff(setup_id)
    assert result == {"started": False, "reason": "setup_expired"}
    assert Client.submissions == 0


@pytest.mark.asyncio
async def test_invalidated_setup_is_blocked_before_binance_client_creation(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory, setup_status="invalidated")
    result = await executor(session_factory, monkeypatch).handoff(setup_id)
    assert result == {"started": False, "reason": "setup_not_eligible"}
    assert Client.submissions == 0


@pytest.mark.asyncio
async def test_immediate_market_fill_is_persisted_truthfully(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    service = executor(session_factory, monkeypatch)
    Client.response = {"orderId": 78, "status": "FILLED", "executedQty": "5"}
    result = await service.handoff(setup_id)
    assert result["status"] == "FILLED"
    with session_factory() as db:
        order = db.scalar(select(ExecutionOrder))
        assert order.execution_state == "filled"
        assert order.filled_at is not None
        assert (
            db.scalar(
                select(func.count(ExecutionEvent.id)).where(
                    ExecutionEvent.event_type == "execution_filled"
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_unexpected_submission_failure_marks_persisted_order_failed(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    service = executor(session_factory, monkeypatch)
    Client.unexpected_error = RuntimeError("unexpected test failure")
    result = await service.handoff(setup_id)
    assert result == {"started": False, "reason": "execution_failed"}
    with session_factory() as db:
        order = db.scalar(select(ExecutionOrder))
        assert order.status == "execution_failed"
        assert order.execution_state == "failed"
        assert order.rejection_reason == "unexpected test failure"
        event = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.event_type == "execution_failed"
            )
        )
        assert event.execution_order_id == order.id


@pytest.mark.asyncio
async def test_missing_quantity_steps_fail_safely_before_order_creation(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    service = executor(session_factory, monkeypatch)
    Client.quantity_filters = [
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0",
            "maxQty": "100",
            "stepSize": "0",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00001",
            "maxQty": "9000",
            "stepSize": "0",
        },
    ]
    result = await service.handoff(setup_id)
    assert result == {"started": False, "reason": "invalid_quantity_step"}
    assert Client.submissions == 0
    with session_factory() as db:
        assert db.scalar(select(func.count(ExecutionOrder.id))) == 0


@pytest.mark.asyncio
async def test_liquidity_reversal_maps_to_enabled_runtime_strategy(
    session_factory, monkeypatch
):
    setup_id = seed(
        session_factory,
        direction="bearish",
        strategy="bearish_liquidity_reversal",
        runtime_strategies=["liquidity_sweep_reversal"],
    )
    settings = execution_settings(
        allowed_execution_strategies="bearish_liquidity_reversal"
    )
    result = await executor(session_factory, monkeypatch, settings).handoff(setup_id)
    assert result["reason"] == "spot_sell_requires_asset_balance"


@pytest.mark.asyncio
async def test_disabled_originating_strategy_is_blocked(session_factory, monkeypatch):
    setup_id = seed(
        session_factory,
        strategy="bullish_liquidity_reversal",
        runtime_strategies=["bos_continuation"],
    )
    settings = execution_settings(
        allowed_execution_strategies="bullish_liquidity_reversal"
    )
    result = await executor(session_factory, monkeypatch, settings).handoff(setup_id)
    assert result["reason"] == "originating_strategy_not_enabled"


@pytest.mark.asyncio
async def test_execution_allowlist_is_a_separate_strategy_gate(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    result = await executor(
        session_factory,
        monkeypatch,
        execution_settings(allowed_execution_strategies="bullish_c_wave"),
    ).handoff(setup_id)
    assert result["reason"] == "execution_strategy_not_allowed"


@pytest.mark.asyncio
async def test_unknown_setup_strategy_is_safely_blocked(session_factory, monkeypatch):
    setup_id = seed(
        session_factory,
        strategy="unknown_strategy",
        runtime_strategies=["bos_continuation"],
    )
    result = await executor(
        session_factory,
        monkeypatch,
        execution_settings(allowed_execution_strategies="unknown_strategy"),
    ).handoff(setup_id)
    assert result["reason"] == "strategy_mapping_missing"


@pytest.mark.asyncio
async def test_c_wave_maps_to_c_wave_runtime_strategy(session_factory, monkeypatch):
    setup_id = seed(
        session_factory,
        direction="bearish",
        strategy="bearish_c_wave",
        runtime_strategies=["c_wave_reversal"],
    )
    settings = execution_settings(allowed_execution_strategies="bearish_c_wave")
    result = await executor(session_factory, monkeypatch, settings).handoff(setup_id)
    assert result["reason"] == "spot_sell_requires_asset_balance"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings_updates,seed_updates,reason",
    [
        ({}, {"automatic": False}, "automatic_trading_disabled"),
        ({}, {"status": "stopped"}, "bot_not_running"),
        ({}, {"paused": True}, "new_entries_paused"),
        ({}, {"kill": True}, "kill_switch_enabled"),
        ({}, {"enabled": False}, "symbol_not_enabled"),
        ({}, {"direction": "bearish"}, "spot_sell_requires_asset_balance"),
    ],
)
async def test_automatic_handoff_safety_skips(
    settings_updates, seed_updates, reason, session_factory, monkeypatch
):
    setup_id = seed(session_factory, **seed_updates)
    result = await executor(
        session_factory, monkeypatch, execution_settings(**settings_updates)
    ).handoff(setup_id)
    assert result == {"started": False, "reason": reason}
    assert Client.submissions == 0
    with session_factory() as db:
        event = db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.event_type == "auto_execution_skipped"
            )
        )
        assert event.metadata_json["reason"] == reason


@pytest.mark.asyncio
async def test_binance_rejection_is_persisted(session_factory, monkeypatch):
    setup_id = seed(session_factory)
    service = executor(session_factory, monkeypatch)
    Client.rejection = BinanceError("test rejection", code=-1013, status=400)
    result = await service.handoff(setup_id)
    assert result["started"] and result["reason"] == "test rejection"
    with session_factory() as db:
        order = db.scalar(select(ExecutionOrder))
        assert (
            order.execution_state == "rejected"
            and order.rejection_reason == "test rejection"
        )


@pytest.mark.asyncio
async def test_invalid_geometry_never_reaches_binance_and_reason_is_persisted(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory, invalid_geometry=True)
    result = await executor(session_factory, monkeypatch).handoff(setup_id)
    assert result["reason"] == "invalid_stop_side" and Client.submissions == 0
    with session_factory() as db:
        event = db.scalar(
            select(ExecutionEvent)
            .where(ExecutionEvent.event_type == "auto_execution_skipped")
            .order_by(ExecutionEvent.id.desc())
        )
        assert "invalid_stop_side" in event.metadata_json["reasons"]


@pytest.mark.asyncio
async def test_unknown_submission_reconciles_by_client_id_without_resubmit(
    session_factory, monkeypatch
):
    setup_id = seed(session_factory)
    service = executor(session_factory, monkeypatch)
    Client.rejection = BinanceError("timeout", unknown=True)
    first = await service.handoff(setup_id)
    second = await service.handoff(setup_id)
    assert first["started"] and first["status"] == "NEW"
    assert second["reason"] == "duplicate_setup_window"
    assert Client.submissions == 1
    with session_factory() as db:
        assert db.scalar(select(ExecutionOrder)).execution_state == "reconciled"


def test_automatic_mode_does_not_put_eligible_setup_in_approval_queue(session_factory):
    seed(session_factory)
    with session_factory() as db:
        assert queue(db) == []


def test_manual_mode_returns_pending_setup_with_symbol(session_factory):
    setup_id = seed(session_factory, automatic=False)
    with session_factory.begin() as db:
        runtime = db.scalar(select(BotRuntimeState))
        runtime.manual_approval_required = True
    with session_factory() as db:
        rows = queue(db)
        assert (
            len(rows) == 1
            and rows[0]["id"] == setup_id
            and rows[0]["symbol"] == "BTCUSDT"
        )


def lifecycle_seed(session_factory, *, manual=False, direction="bullish"):
    start = datetime(2026, 8, 13, tzinfo=timezone.utc)
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
        candle_ids = []
        for index, low in enumerate((Decimal("102"), Decimal("99"), Decimal("99"))):
            opened = start + timedelta(minutes=index)
            candle, _ = upsert_candle(
                db,
                symbol.id,
                "1m",
                CandleData(
                    open_time=opened,
                    close_time=opened
                    + timedelta(minutes=1)
                    - timedelta(milliseconds=1),
                    open=Decimal("102"),
                    high=Decimal("103"),
                    low=low,
                    close=Decimal("101"),
                    volume=Decimal("10"),
                    quote_volume=Decimal("1000"),
                    trade_count=5,
                    taker_buy_base_volume=Decimal("5"),
                    taker_buy_quote_volume=Decimal("500"),
                    is_closed=True,
                ),
            )
            candle_ids.append(candle.id)
        setup = TradeSetup(
            symbol_id=symbol.id,
            direction=direction,
            strategy="bullish_continuation",
            status="watching",
            higher_timeframe="5m",
            setup_timeframe="1m",
            entry_timeframe="1m",
            structure_event_id=1,
            entry_min=Decimal("99"),
            entry_max=Decimal("101"),
            preferred_entry=Decimal("100"),
            stop_loss=Decimal("95") if direction == "bullish" else Decimal("105"),
            invalidation_price=Decimal("95")
            if direction == "bullish"
            else Decimal("105"),
            take_profit_1=Decimal("105") if direction == "bullish" else Decimal("95"),
            take_profit_2=Decimal("110") if direction == "bullish" else Decimal("90"),
            take_profit_3=Decimal("115") if direction == "bullish" else Decimal("85"),
            confidence_score=90,
            expires_at=start + timedelta(hours=1),
            detected_at=start,
        )
        db.add(setup)
        db.add(
            BotRuntimeState(
                status="running",
                environment="testnet",
                automatic_trading_enabled=True,
                manual_approval_required=manual,
                pause_new_entries=False,
                kill_switch_enabled=False,
                enabled_symbols_json=["BTCUSDT"],
                enabled_timeframes_json=["1m"],
                enabled_strategies_json=["bullish_continuation"],
            )
        )
        db.flush()
        return setup.id, candle_ids


@pytest.mark.asyncio
async def test_watching_entry_touch_routes_exactly_one_automatic_handoff(
    session_factory, monkeypatch
):
    setup_id, candle_ids = lifecycle_seed(session_factory)
    calls = []

    async def handoff(self, routed_id, **kwargs):
        calls.append(routed_id)
        return {"started": True}

    monkeypatch.setattr(AutomaticTestnetExecutor, "handoff", handoff)
    await process_closed_candle(
        candle_ids[1], broadcast=False, session_factory=session_factory
    )
    await process_closed_candle(
        candle_ids[2], broadcast=False, session_factory=session_factory
    )
    assert calls == [setup_id]
    with session_factory() as db:
        setup = db.get(TradeSetup, setup_id)
        eligible = list(db.scalars(select(ExecutionEvent)))
        logs = list(
            db.scalars(select(BotLog).where(BotLog.event_type == "execution_eligible"))
        )
        assert setup.status == "triggered" and setup.triggered_at is not None
        assert len(logs) == 1 and not eligible


@pytest.mark.asyncio
async def test_manual_triggered_setup_is_queued_without_auto_handoff(
    session_factory, monkeypatch
):
    setup_id, candle_ids = lifecycle_seed(session_factory, manual=True)
    calls = []

    async def handoff(self, routed_id, **kwargs):
        calls.append(routed_id)

    monkeypatch.setattr(AutomaticTestnetExecutor, "handoff", handoff)
    await process_closed_candle(
        candle_ids[1], broadcast=False, session_factory=session_factory
    )
    assert calls == []
    with session_factory() as db:
        rows = queue(db)
        assert (
            len(rows) == 1
            and rows[0]["id"] == setup_id
            and rows[0]["status"] == "triggered"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_change",
    [
        {"pause_new_entries": True},
        {"kill_switch_enabled": True},
        {"status": "stopped"},
        {"automatic_trading_enabled": False},
    ],
)
async def test_triggered_setup_is_not_mislabeled_eligible_when_routing_disabled(
    runtime_change, session_factory, monkeypatch
):
    _, candle_ids = lifecycle_seed(session_factory)
    with session_factory.begin() as db:
        runtime = db.scalar(select(BotRuntimeState))
        for field, value in runtime_change.items():
            setattr(runtime, field, value)
    calls = []

    async def handoff(self, routed_id, **kwargs):
        calls.append(routed_id)

    monkeypatch.setattr(AutomaticTestnetExecutor, "handoff", handoff)
    await process_closed_candle(
        candle_ids[1], broadcast=False, session_factory=session_factory
    )
    assert calls == []
    with session_factory() as db:
        assert (
            db.scalar(
                select(func.count(BotLog.id)).where(
                    BotLog.event_type == "execution_eligible"
                )
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["expired", "invalidated"])
async def test_terminal_lifecycle_transition_never_routes_execution(
    terminal_status, session_factory, monkeypatch
):
    setup_id, candle_ids = lifecycle_seed(session_factory)
    with session_factory.begin() as db:
        setup = db.get(TradeSetup, setup_id)
        if terminal_status == "expired":
            setup.expires_at = datetime(2026, 8, 13, tzinfo=timezone.utc)
        else:
            setup.invalidation_price = Decimal("100")
    calls = []

    async def handoff(self, routed_id, **kwargs):
        calls.append(routed_id)

    monkeypatch.setattr(AutomaticTestnetExecutor, "handoff", handoff)
    await process_closed_candle(
        candle_ids[1], broadcast=False, session_factory=session_factory
    )
    assert calls == []
    with session_factory() as db:
        assert db.get(TradeSetup, setup_id).status == terminal_status
