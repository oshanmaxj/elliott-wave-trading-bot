from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.trading.backtest import closed_candles_at
from app.trading.execution import execution_fee, pnl, position_size, slipped_price
from app.trading.metrics import calculate_metrics
from app.trading.paper import manual_close
from app.models import PaperAccount, PaperPosition


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


def test_paper_close_applies_profit_fees_slippage_and_risk(session_factory):
    d = Decimal
    with session_factory() as db:
        account = PaperAccount(
            name="test", starting_balance=d("10000"), balance=d("10000"),
            equity=d("10000"), realized_pnl=0, unrealized_pnl=0,
            max_equity=d("10000"), drawdown_pct=0, risk_per_trade_pct=1,
            max_daily_loss_pct=3, is_active=True,
        )
        db.add(account)
        db.flush()
        position = PaperPosition(
            account_id=account.id, trade_setup_id=1, symbol_id=1,
            direction="bullish", status="open", entry_price=d("100"),
            quantity=d("10"), initial_quantity=d("10"), risk_amount=d("100"),
            stop_loss=d("90"), tp1=d("105"), tp2=d("110"), tp3=d("115"),
            realized_pnl=0, realized_r=0, fees=0, slippage=0,
            taker_fee_pct=d(".05"), slippage_bps=d("10"),
        )
        db.add(position)
        db.flush()
        manual_close(db, position, d("110"), datetime(2025, 1, 2, tzinfo=timezone.utc))
        assert position.status == "closed"
        assert position.exit_price == d("109.890")
        assert position.fees > 0 and position.slippage > 0
        assert account.balance == d("10000") + position.realized_pnl
        assert position.realized_r == position.realized_pnl / d("100")


def test_paper_module_has_no_exchange_order_client():
    import app.trading.paper as paper
    assert not any("exchange" in name or "order_client" in name for name in vars(paper))
