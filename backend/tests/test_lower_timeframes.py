from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.api.bot import strategy_diagnostics
from app.core.config import Settings
from app.core.constants import BINANCE_INTERVALS, TIMEFRAME_MS, TIMEFRAMES
from app.database.session import get_db
from app.execution.service import ExecutionRiskEngine, setup_fingerprint
from app.market_data.binance_rest import BinanceRESTClient
from app.market_data.binance_ws import BinanceWebSocketManager
from app.models import BotLog, BotRuntimeState, ExecutionOrder, FVGZone, MarketStructureEvent, OrderBlock, SwingPoint, TradeSetup
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData, RuntimeSettings
from app.services.historical_backfill import HistoricalBackfillService, historical_backfill
from app.services.pipeline import process_closed_candle


def candle(open_time, timeframe):
    step = timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    return CandleData(
        open_time=open_time,
        close_time=open_time + step - timedelta(milliseconds=1),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("10"),
        quote_volume=Decimal("1000"),
        trade_count=10,
        taker_buy_base_volume=Decimal("5"),
        taker_buy_quote_volume=Decimal("500"),
        is_closed=True,
    )


def test_central_timeframes_validate_one_and_five_minutes():
    assert TIMEFRAMES == ("1m", "5m", "15m", "1h", "4h")
    assert TIMEFRAME_MS["1m"] == 60_000
    assert TIMEFRAME_MS["5m"] == 300_000
    assert BINANCE_INTERVALS["1m"] == "1m"
    assert BINANCE_INTERVALS["5m"] == "5m"
    assert Settings(default_timeframes="1m,5m,15m,1h,4h").default_timeframes == list(
        TIMEFRAMES
    )
    assert RuntimeSettings(enabled_timeframes=["1m", "5m"]).enabled_timeframes == [
        "1m",
        "5m",
    ]
    assert RuntimeSettings().enabled_timeframes == ["15m", "1h", "4h"]


def test_binance_stream_mapping_is_complete_and_deduplicated():
    manager = BinanceWebSocketManager()
    manager.settings = SimpleNamespace(
        default_symbols=["BTCUSDT", "ETHUSDT"],
        default_timeframes=["1m", "5m", "1m", "15m", "1h", "4h"],
    )
    assert len(manager.streams) == 10
    assert "btcusdt@kline_1m" in manager.streams
    assert "ethusdt@kline_5m" in manager.streams


@pytest.mark.asyncio
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
async def test_every_enabled_closed_timeframe_reaches_strategy_evaluation(session_factory, timeframe):
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, "BTCUSDT")
        row, _ = upsert_candle(db, symbol.id, timeframe, candle(opened, timeframe))
        db.add(BotRuntimeState(status="running", enabled_symbols_json=["BTCUSDT"], enabled_timeframes_json=list(TIMEFRAMES)))
        candle_id = row.id
    result = await process_closed_candle(candle_id, broadcast=False, session_factory=session_factory)
    assert result["processed"] is True
    with session_factory() as db:
        evaluation = db.query(BotLog).filter_by(event_type="strategy_evaluation").one()
        assert evaluation.context_json["timeframe"] == timeframe


def test_strategy_diagnostics_aggregates_pipeline_events(session_factory):
    with session_factory.begin() as db:
        for event_type, context in (
            ("closed_candle_processed", {}),
            ("strategy_evaluation", {"no_candidate_reasons": ["no_structure_break", "no_fvg"]}),
            ("candidate_generated", {}),
            ("candidate_rejected", {"reason": "invalid_stop_side"}),
            ("setup_persisted", {}),
        ):
            db.add(BotLog(level="INFO", service="strategy_pipeline", event_type=event_type, message=event_type, context_json=context))
    with session_factory() as db:
        result = strategy_diagnostics("24h", db)
    assert result["closed_candles_processed"] == 1
    assert result["strategy_evaluations"] == 1
    assert result["candidates_generated"] == 1
    assert result["setups_persisted"] == 1
    assert result["rejection_reasons"] == {"invalid_stop_side": 1}
    assert result["no_candidate_reasons"] == {"no_structure_break": 1, "no_fvg": 1}
    assert result["analysis_activity"]["swing_points_created"] == 0


@pytest.mark.asyncio
async def test_insufficient_history_is_observable(session_factory):
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, "BTCUSDT")
        row, _ = upsert_candle(db, symbol.id, "1m", candle(opened, "1m"))
        candle_id = row.id
    await process_closed_candle(candle_id, broadcast=False, session_factory=session_factory)
    with session_factory() as db:
        evaluation = db.query(BotLog).filter_by(event_type="strategy_evaluation").one()
        assert "insufficient_candles" in evaluation.context_json["no_candidate_reasons"]
        assert "no_confirmed_swing" in evaluation.context_json["no_candidate_reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol_name", ["BTCUSDT", "ETHUSDT"])
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
async def test_fresh_closed_candles_can_persist_every_analysis_stage(
    session_factory, symbol_name, timeframe
):
    prices = [
        (101, 99, 100, 100), (102, 99, 100, 101), (103, 99, 101, 102),
        (106, 100, 102, 104), (108, 102, 104, 106), (110, 103, 106, 108),
        (109, 102, 108, 105), (108, 100, 105, 103), (106, 97, 103, 100),
        (104, 94, 100, 97), (102, 90, 97, 93), (104, 92, 93, 96),
        (106, 94, 96, 100), (108, 97, 100, 104), (110, 100, 104, 108),
        (112, 103, 108, 110), (111, 104, 110, 107), (109, 101, 107, 104),
        (108, 99, 104, 102), (107, 98, 102, 100), (106, 97, 100, 99),
        (114, 107, 108, 113), (116, 112, 113, 115),
    ]
    opened = datetime(2026, 8, 1, tzinfo=timezone.utc)
    step = timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    candle_ids = []
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, symbol_name)
        for index, (high, low, open_price, close) in enumerate(prices):
            data = candle(opened + index * step, timeframe)
            data.open = Decimal(open_price)
            data.high = Decimal(high)
            data.low = Decimal(low)
            data.close = Decimal(close)
            row, _ = upsert_candle(db, symbol.id, timeframe, data)
            candle_ids.append(row.id)
    for candle_id in candle_ids:
        await process_closed_candle(candle_id, broadcast=False, session_factory=session_factory)
    with session_factory() as db:
        assert db.query(SwingPoint).count() >= 2
        assert db.query(MarketStructureEvent).count() >= 1
        assert db.query(FVGZone).count() >= 1
        assert db.query(OrderBlock).count() >= 1
        assert db.query(TradeSetup).count() >= 1


@pytest.mark.asyncio
async def test_rest_interval_mapping_and_lower_timeframe_persistence(
    session_factory, monkeypatch
):
    client = BinanceRESTClient("https://example.invalid")
    captured = []

    async def request(params):
        captured.append(params)
        return []

    monkeypatch.setattr(client, "_request", request)
    await client.fetch_historical_klines("BTCUSDT", "1m")
    await client.fetch_historical_klines("BTCUSDT", "5m")
    assert [row["interval"] for row in captured] == ["1m", "5m"]

    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, "BTCUSDT")
        first, created = upsert_candle(db, symbol.id, "1m", candle(opened, "1m"))
        repeated, created_again = upsert_candle(
            db, symbol.id, "1m", candle(opened, "1m")
        )
        assert created and not created_again and first.id == repeated.id


@pytest.mark.asyncio
async def test_lower_timeframe_backfill_paginates_and_uses_retention_policy(
    session_factory, monkeypatch
):
    class Client:
        def __init__(self):
            self.calls = []

        async def fetch_historical_klines(
            self, symbol, timeframe, start, end, limit
        ):
            self.calls.append((timeframe, start, end, limit))
            step = timedelta(milliseconds=TIMEFRAME_MS[timeframe])
            rows = []
            while start <= end and len(rows) < 2:
                rows.append(candle(start, timeframe))
                start += step
            return rows

    monkeypatch.setattr(
        "app.services.historical_backfill.log_event", lambda *args, **kwargs: None
    )
    service = HistoricalBackfillService(Client(), session_factory)
    assert service.retention_days("1m") == 30
    assert service.retention_days("5m") == 90
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = await service.run(
        "BTCUSDT", "1m", start=start, end=start + timedelta(minutes=5)
    )
    assert result["coverage"]["coverage_complete"]
    assert result["processed_batches"] == 3


def test_market_analysis_endpoints_accept_lower_timeframes(session_factory, monkeypatch):
    monkeypatch.setattr(historical_backfill, "session_factory", session_factory)
    monkeypatch.setattr(
        historical_backfill, "verify", lambda *args, **kwargs: {"coverage_complete": True}
    )
    with session_factory.begin() as db:
        ensure_symbol(db, "BTCUSDT")
    app = FastAPI()
    app.include_router(router)

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    api = TestClient(app)
    for timeframe in ("1m", "5m"):
        for path in (
            "/api/candles",
            "/api/analysis/latest",
            "/api/fvg",
            "/api/liquidity",
            "/api/order-blocks",
            "/api/structure-score",
            "/api/liquidity-sweeps",
            "/api/trade-setups",
            "/api/elliott-wave/counts",
            "/api/market/state",
        ):
            response = api.get(
                path, params={"symbol": "BTCUSDT", "timeframe": timeframe}
            )
            assert response.status_code != 422, (path, response.text)


def execution_settings():
    return SimpleNamespace(
        binance_execution_enabled=False,
        execution_mode="disabled",
        allowed_execution_symbols=["BTCUSDT"],
        allowed_execution_strategies=["bullish_continuation"],
        min_execution_confidence=Decimal("75"),
        max_risk_per_trade_pct=Decimal("0.25"),
        max_symbol_exposure_pct=Decimal("10"),
        max_open_positions=1,
    )


def setup(timeframe="1m", detected=None):
    return SimpleNamespace(
        id=101,
        setup_timeframe=timeframe,
        strategy="bullish_continuation",
        direction="bullish",
        status="ready",
        confidence_score=Decimal("90"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        detected_at=detected or datetime(2026, 1, 1, 12, 7, tzinfo=timezone.utc),
        preferred_entry=Decimal("100"),
        stop_loss=Decimal("99"),
        entry_min=Decimal("99"),
        entry_max=Decimal("101"),
        take_profit_1=Decimal("102"),
        take_profit_2=Decimal("103"),
        take_profit_3=Decimal("104"),
    )


def test_timeframe_selection_risk_checks_and_duplicate_fingerprint(session_factory):
    symbol = SimpleNamespace(id=1, symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    info = {
        "status": "TRADING",
        "isSpotTradingAllowed": True,
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.0001", "maxQty": "100", "stepSize": "0.0001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
        ],
    }
    account = {"balances": [{"asset": "USDT", "free": "10000"}]}
    with session_factory() as db:
        db.add(BotRuntimeState(enabled_timeframes_json=["5m", "15m"]))
        db.commit()
        one_minute = setup("1m")
        blocked = ExecutionRiskEngine(execution_settings()).evaluate(
            db, one_minute, symbol, account, Decimal("100"), info
        )
        assert "timeframe_not_enabled" in blocked.reasons
        assert "execution_disabled" in blocked.reasons

        five_minute = setup("5m")
        allowed_timeframe = ExecutionRiskEngine(execution_settings()).evaluate(
            db, five_minute, symbol, account, Decimal("100"), info
        )
        assert "timeframe_not_enabled" not in allowed_timeframe.reasons

        fingerprint = setup_fingerprint(symbol.symbol, five_minute)
        db.add(
            ExecutionOrder(
                environment="testnet",
                symbol_id=1,
                trade_setup_id=999,
                client_order_id="dedup-test",
                setup_fingerprint=fingerprint,
                side="BUY",
                order_type="MARKET",
                requested_quantity=Decimal("1"),
                status="NEW",
                execution_state="acknowledged",
            )
        )
        db.commit()
        duplicate = ExecutionRiskEngine(execution_settings()).evaluate(
            db, five_minute, symbol, account, Decimal("100"), info
        )
        assert "duplicate_setup_window" in duplicate.reasons

    same_window_other_timeframe = setup("1m")
    assert setup_fingerprint("BTCUSDT", five_minute) == setup_fingerprint(
        "BTCUSDT", same_window_other_timeframe
    )
    assert setup_fingerprint("ETHUSDT", five_minute) != fingerprint
