from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.api.paper_forward import confidence_grouped, stats
from app.models import Candle, LivePosition, PaperForwardTrade, Symbol, TradeSetup
from app.trading.paper_forward import SOURCE, comparison_rows, enroll_setup, process_trade_candle

D = Decimal
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def setup(**changes):
    values = dict(id=1, symbol_id=1, direction="bullish", strategy="SMC", setup_timeframe="1h",
        entry_min=D("99"), entry_max=D("101"), preferred_entry=D("100"), stop_loss=D("90"),
        invalidation_price=D("90"), take_profit_1=D("115"), take_profit_2=D("120"),
        take_profit_3=D("130"), confidence_score=D("75"), rejection_reasons_json=[],
        detected_at=NOW, expires_at=NOW+timedelta(hours=4))
    values.update(changes)
    return SimpleNamespace(**values)


def trade(**changes):
    values = dict(id=1, setup_id=1, symbol_id=1, symbol="BTCUSDT", strategy="SMC", direction="bullish",
        timeframe="1h", confidence_score=D("75"), simulated_entry=D("100"), entry_min=D("99"),
        entry_max=D("101"), stop_loss=D("90"), active_stop=D("90"), take_profit_1=D("115"),
        take_profit_2=D("120"), take_profit_3=D("130"), next_target=1, initial_quantity=D("0.1"),
        remaining_quantity=D("0.1"), risk_amount=D("1"), opened_at=None, closed_at=None, exit_price=None,
        exit_reason=None, realized_r=D("0"), realized_pnl=D("0"), fees=D("0"), fee_rate_pct=D("0"),
        status="waiting_entry", max_favorable_excursion=D("0"), max_adverse_excursion=D("0"),
        mfe_r=D("0"), mae_r=D("0"), holding_bars=0, is_ambiguous=False, market_data_source=SOURCE)
    values.update(changes)
    return SimpleNamespace(**values)


def candle(hour, high, low, close=100):
    opened=NOW+timedelta(hours=hour)
    return SimpleNamespace(symbol_id=1,timeframe="1h",is_closed=True,open_time=opened,
        close_time=opened+timedelta(hours=1),high=D(str(high)),low=D(str(low)),close=D(str(close)))


def test_long_tp_hit():
    row=trade(take_profit_2=None,take_profit_3=None)
    process_trade_candle(row,setup(take_profit_2=None,take_profit_3=None),candle(1,116,99))
    assert row.status == "closed" and row.exit_reason == "tp1" and row.realized_r == D("1.5")


def test_long_sl_hit():
    row=trade()
    process_trade_candle(row,setup(),candle(1,101,89))
    assert row.status == "closed" and row.realized_r == D("-1")


def test_same_candle_sl_tp_is_conservative_and_ambiguous():
    row=trade()
    process_trade_candle(row,setup(),candle(1,116,89))
    assert row.exit_reason == "stop_loss" and row.is_ambiguous and row.realized_r == D("-1")


def test_expired_setup_never_opens():
    row=trade()
    process_trade_candle(row,setup(expires_at=NOW),candle(1,110,95))
    assert row.status == "expired" and row.opened_at is None


def test_duplicate_setup_prevention(session_factory):
    with session_factory() as db:
        db.add(Symbol(id=1,exchange="binance",symbol="BTCUSDT",base_asset="BTC",quote_asset="USDT",market_type="spot"))
        row=TradeSetup(id=1,symbol_id=1,direction="bullish",strategy="SMC",status="ready",higher_timeframe="4h",setup_timeframe="1h",entry_timeframe="1h",structure_event_id=1,entry_min=99,entry_max=101,preferred_entry=100,stop_loss=90,invalidation_price=90,take_profit_1=115,take_profit_2=120,take_profit_3=130,confidence_score=75,score_breakdown_json={},setup_conditions_json={},rejection_reasons_json=[],expires_at=NOW+timedelta(hours=4),detected_at=NOW)
        db.add(row); db.flush()
        assert enroll_setup(db,row).id == enroll_setup(db,row).id
        assert db.query(PaperForwardTrade).count() == 1


def closed(strategy="SMC", confidence=75, pnl="1", r="1", ambiguous=False, hour=1):
    return trade(strategy=strategy,confidence_score=D(str(confidence)),status="closed",realized_pnl=D(pnl),realized_r=D(r),is_ambiguous=ambiguous,opened_at=NOW,closed_at=NOW+timedelta(hours=hour))


def test_multiple_strategies_and_confidence_buckets():
    rows=[closed("SMC",55),closed("ELLIOTT",95)]
    buckets=confidence_grouped(rows)
    assert buckets[0]["total_trades"] == 1 and buckets[-1]["total_trades"] == 1
    assert {r.strategy for r in rows} == {"SMC","ELLIOTT"}


def test_drawdown_and_consecutive_losses():
    result=stats([closed(pnl="2",r="2"),closed(pnl="-1",r="-1"),closed(pnl="-2",r="-2")])
    assert result["max_drawdown_r"] == 3 and result["consecutive_losses"] == 2


def test_profit_factor_and_ambiguous_headline_exclusion():
    result=stats([closed(pnl="2",r="2"),closed(pnl="-1",r="-1"),closed(pnl="-3",r="-3",ambiguous=True)])
    assert result["profit_factor"] == D("0.5") and result["wins"] == 1 and result["losses"] == 1 and result["ambiguous_excluded"] == 1


def test_testnet_comparison(session_factory):
    with session_factory() as db:
        db.add(Symbol(id=1,exchange="binance",symbol="BTCUSDT",base_asset="BTC",quote_asset="USDT",market_type="spot"))
        db.add(LivePosition(id=1,exchange="binance",environment="testnet",symbol_id=1,originating_trade_setup_id=1,direction="bullish",status="closed",base_quantity=1,remaining_quantity=0,average_entry=100,stop_loss=90,protection_status="closed",realized_pnl=-15,unrealized_pnl=0,total_fees=0,opened_at=NOW,closed_at=NOW+timedelta(hours=1),exit_reason="stop_loss",exit_price=85))
        db.flush()
        result=comparison_rows(db,[closed(pnl="-1",r="-1")])[0]
        assert result["testnet_slippage"] == 5 and result["difference_in_r"] == D("-0.5")


def test_production_candle_source_only():
    row=trade()
    process_trade_candle(row,setup(),candle(1,101,99))
    assert row.market_data_source == "binance_production_spot_db"
