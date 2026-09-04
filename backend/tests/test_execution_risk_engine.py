from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.config import Settings
from app.execution.service import ExecutionRiskEngine
from app.models import BotRuntimeState, LivePosition, Symbol, TradeSetup


def settings(**updates):
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
    risk_config_json=None,
    confidence=Decimal("90"),
    tp1=Decimal("110"),
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
            direction="bullish",
            strategy="bullish_continuation",
            status="ready",
            higher_timeframe="5m",
            setup_timeframe="1m",
            entry_timeframe="1m",
            structure_event_id=1,
            entry_min=Decimal("99"),
            entry_max=Decimal("101"),
            preferred_entry=Decimal("100"),
            stop_loss=Decimal("95"),
            invalidation_price=Decimal("95"),
            take_profit_1=tp1,
            take_profit_2=Decimal("115"),
            take_profit_3=Decimal("120"),
            risk_reward_1=2,
            risk_reward_2=3,
            risk_reward_3=4,
            confidence_score=confidence,
            expires_at=now + timedelta(hours=1),
            detected_at=now,
        )
        db.add(setup)
        db.add(
            BotRuntimeState(
                status="running",
                environment="testnet",
                automatic_trading_enabled=True,
                manual_approval_required=False,
                pause_new_entries=False,
                kill_switch_enabled=False,
                enabled_symbols_json=["BTCUSDT"],
                enabled_timeframes_json=["1m"],
                enabled_strategies_json=["bos_continuation"],
                risk_config_json=risk_config_json if risk_config_json is not None else {},
            )
        )
        db.flush()
        return symbol.id, setup.id


def account(equity="10000"):
    return {"balances": [{"asset": "USDT", "free": equity}, {"asset": "BTC", "free": "0"}]}


def symbol_info():
    return {
        "status": "TRADING",
        "isSpotTradingAllowed": True,
        "orderTypes": ["MARKET"],
        "filters": [
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
            {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
        ],
    }


def add_active_position(
    session_factory, symbol_id, setup_id, *, quantity=Decimal("1"), entry=Decimal("100")
):
    with session_factory.begin() as db:
        db.add(
            LivePosition(
                environment="testnet",
                symbol_id=symbol_id,
                originating_trade_setup_id=setup_id,
                direction="long",
                status="open",
                base_quantity=quantity,
                remaining_quantity=quantity,
                average_entry=entry,
                stop_loss=Decimal("90"),
                take_profit_1=Decimal("110"),
                protection_status="protected",
                opened_at=datetime.now(timezone.utc),
            )
        )


def evaluate(session_factory, symbol_id, setup_id, engine_settings):
    with session_factory() as db:
        symbol = db.get(Symbol, symbol_id)
        setup = db.get(TradeSetup, setup_id)
        return ExecutionRiskEngine(engine_settings).evaluate(
            db, setup, symbol, account(), Decimal("100"), symbol_info()
        )


def test_risk_per_trade_pct_override_increases_computed_risk_and_quantity(session_factory):
    symbol_id, setup_id = seed(session_factory, risk_config_json={"risk_per_trade_pct": "1.0"})
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert decision.risk_amount == Decimal("100")
    assert decision.calculated_quantity == Decimal("20")
    assert decision.adjusted_quantity == Decimal("10")


def test_default_risk_per_trade_pct_falls_back_to_env_setting(session_factory):
    symbol_id, setup_id = seed(session_factory, risk_config_json={})
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert decision.risk_amount == Decimal("25")
    assert decision.calculated_quantity == Decimal("5")


def test_max_open_positions_default_blocks_second_position(session_factory):
    symbol_id, setup_id = seed(session_factory, risk_config_json={})
    add_active_position(session_factory, symbol_id, setup_id)
    decision = evaluate(session_factory, symbol_id, setup_id, settings(max_open_positions=1))
    assert "max_open_positions_reached" in decision.reasons


def test_max_open_positions_override_allows_more_positions(session_factory):
    symbol_id, setup_id = seed(
        session_factory, risk_config_json={"max_open_positions": "5"}
    )
    add_active_position(session_factory, symbol_id, setup_id)
    decision = evaluate(session_factory, symbol_id, setup_id, settings(max_open_positions=1))
    assert "max_open_positions_reached" not in decision.reasons


def test_minimum_confidence_override_can_reject_a_setup_the_env_default_allows(
    session_factory,
):
    symbol_id, setup_id = seed(
        session_factory,
        confidence=Decimal("80"),
        risk_config_json={"minimum_confidence": "85"},
    )
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert "confidence_below_minimum" in decision.reasons


def test_minimum_confidence_override_can_allow_a_setup_the_env_default_rejects(
    session_factory,
):
    symbol_id, setup_id = seed(
        session_factory,
        confidence=Decimal("80"),
        risk_config_json={"minimum_confidence": "70"},
    )
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert "confidence_below_minimum" not in decision.reasons


def test_minimum_rr_override_can_reject_a_setup_at_2r(session_factory):
    # entry 100, stop 95 -> risk 5; tp1 110 -> reward 10; rr == 2
    symbol_id, setup_id = seed(session_factory, risk_config_json={"minimum_rr": "3"})
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert "invalid_rr" in decision.reasons


def test_minimum_rr_override_can_allow_a_setup_at_2r(session_factory):
    symbol_id, setup_id = seed(session_factory, risk_config_json={"minimum_rr": "1"})
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert "invalid_rr" not in decision.reasons


def test_max_total_exposure_pct_fires_when_cap_exceeded(session_factory):
    symbol_id, setup_id = seed(
        session_factory,
        risk_config_json={"risk_per_trade_pct": "1.0", "max_total_exposure_pct": "5"},
    )
    add_active_position(
        session_factory, symbol_id, setup_id, quantity=Decimal("4"), entry=Decimal("100")
    )
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert decision.estimated_notional == Decimal("1000")
    assert "max_total_exposure_reached" in decision.reasons


def test_max_total_exposure_pct_does_not_fire_under_a_generous_cap(session_factory):
    symbol_id, setup_id = seed(
        session_factory,
        risk_config_json={"risk_per_trade_pct": "1.0", "max_total_exposure_pct": "50"},
    )
    add_active_position(
        session_factory, symbol_id, setup_id, quantity=Decimal("4"), entry=Decimal("100")
    )
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert "max_total_exposure_reached" not in decision.reasons


def test_malformed_risk_config_falls_back_to_env_defaults_without_raising(session_factory):
    symbol_id, setup_id = seed(
        session_factory,
        risk_config_json={
            "risk_per_trade_pct": "not-a-number",
            "max_open_positions": "-5",
            "minimum_confidence": "",
            "max_symbol_exposure_pct": None,
            "minimum_rr": "abc",
            "max_total_exposure_pct": "also-bad",
        },
    )
    decision = evaluate(session_factory, symbol_id, setup_id, settings())
    assert decision.risk_amount == Decimal("25")
    assert decision.calculated_quantity == Decimal("5")
    assert "confidence_below_minimum" not in decision.reasons
    assert "invalid_rr" not in decision.reasons
