from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.trading.backtest import closed_candles_at
from app.trading.execution import execution_fee, pnl, position_size, slipped_price
from app.trading.metrics import calculate_metrics


def candle(hours, timeframe="15m", closed=True):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        close_time=start + timedelta(hours=hours), timeframe=timeframe,
        is_closed=closed,
    )


def test_future_candles_cannot_affect_earlier_visibility():
    rows = [candle(1), candle(2), candle(3)]
    visible = closed_candles_at(rows, rows[0].close_time)
    assert visible == [rows[0]]
    rows.append(candle(4))
    assert closed_candles_at(rows, rows[0].close_time) == visible


def test_htf_candle_is_available_only_after_close():
    htf = candle(4, "4h")
    assert htf not in closed_candles_at([htf], htf.close_time - timedelta(microseconds=1))
    assert htf in closed_candles_at([htf], htf.close_time)


def test_execution_costs_and_risk_are_deterministic():
    d = Decimal
    risk, quantity = position_size(d("10000"), d("1"), d("100"), d("95"), d("1"))
    assert risk == d("100") and quantity == d("20")
    entry = slipped_price(d("100"), "bullish", d("10"), True)
    exit_price = slipped_price(d("110"), "bullish", d("10"), False)
    fees = execution_fee(entry, quantity, d(".05")) + execution_fee(exit_price, quantity, d(".05"))
    assert entry == d("100.1") and exit_price == d("109.890")
    assert pnl("bullish", entry, exit_price, quantity, fees) < pnl("bullish", d("100"), d("110"), quantity)


def test_zero_trade_metrics_are_reproducible_and_explainable_shape():
    first = calculate_metrics([], [], Decimal("10000"))
    second = calculate_metrics([], [], Decimal("10000"))
    assert first == second
    assert first["extended"]["total_trades"] == 0
    assert first["equity_curve"] == [{"index": 0, "equity": 10000.0, "drawdown_pct": 0.0, "drawdown": 0.0}]
