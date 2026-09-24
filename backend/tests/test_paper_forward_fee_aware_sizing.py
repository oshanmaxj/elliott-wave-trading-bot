"""Fee-aware paper-forward position sizing.

Covers the fix for the bug where enroll_setup() sized new trades using only
raw price distance (quantity = 1 / distance), ignoring the entry and exit
trading fees the engine deducts later. On tight stops this produced enormous
notional sizes whose fees dwarfed the intended 1R risk (observed in
production as a nominal 1R hard-stop loss realizing as -24.73R).

These tests import the private helpers (_fee_aware_quantity, _record_exit)
directly, following the same pattern already used for other paper-forward
internals (e.g. _wave3_ha_1m_update) in test_elliott_wave3_heikin_ashi.py.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import Symbol, TradeSetup
from app.trading.execution import execution_fee, pnl
from app.trading.paper_forward import _fee_aware_quantity, _record_exit, enroll_setup, process_trade_candle
from test_paper_forward import candle, setup, trade

D = Decimal
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_execution_fee_uses_percent_units_matching_fee_rate_pct_default():
    # fee_rate_pct is percent units: 0.1 means 0.1%, not 0.1 (i.e. 10%).
    assert execution_fee(D("100"), D("1"), D("0.1")) == D("0.1")


def test_fee_aware_quantity_normal_stop_distance_totals_approximately_risk_amount():
    entry, stop, fee_rate_pct = D("100"), D("90"), D("0.1")
    quantity = _fee_aware_quantity(entry, stop, D("1"), fee_rate_pct)
    planned_stop_loss = (
        quantity * abs(entry - stop)
        + execution_fee(entry, quantity, fee_rate_pct)
        + execution_fee(stop, quantity, fee_rate_pct)
    )
    assert abs(planned_stop_loss - D("1")) < D("0.0000001")


def test_fee_aware_quantity_tight_stop_no_longer_produces_enormous_quantity():
    entry, stop, fee_rate_pct = D("65000"), D("64999.5"), D("0.1")  # $0.50 stop distance on a $65k asset
    old_quantity = D("1") / abs(entry - stop)  # the old, buggy sizing
    new_quantity = _fee_aware_quantity(entry, stop, D("1"), fee_rate_pct)
    assert new_quantity < old_quantity / D("100")  # dramatically smaller notional
    planned_stop_loss = (
        new_quantity * abs(entry - stop)
        + execution_fee(entry, new_quantity, fee_rate_pct)
        + execution_fee(stop, new_quantity, fee_rate_pct)
    )
    assert abs(planned_stop_loss - D("1")) < D("0.0000001")
    # Sanity check against the documented production symptom: the old sizing's
    # planned loss at the "1R" stop was over 260R, not ~1R.
    old_planned_stop_loss = (
        old_quantity * abs(entry - stop)
        + execution_fee(entry, old_quantity, fee_rate_pct)
        + execution_fee(stop, old_quantity, fee_rate_pct)
    )
    assert old_planned_stop_loss > D("100")


def test_fee_aware_quantity_handles_invalid_denominators_safely():
    assert _fee_aware_quantity(D("100"), D("100"), D("1"), D("0.1")) is None  # zero distance
    assert _fee_aware_quantity(D("100"), D("90"), D("0"), D("0.1")) is None  # zero risk_amount
    assert _fee_aware_quantity(D("100"), D("90"), D("-1"), D("0.1")) is None  # negative risk_amount
    assert _fee_aware_quantity(D("100"), D("90"), D("1"), D("-1")) is None  # negative fee rate


def test_hard_stop_exit_realizes_approximately_negative_one_r_after_both_fees():
    fee_rate_pct = D("0.1")
    entry, stop = D("100"), D("90")
    quantity = _fee_aware_quantity(entry, stop, D("1"), fee_rate_pct)
    row = trade(initial_quantity=quantity, remaining_quantity=quantity, fee_rate_pct=fee_rate_pct)
    s = setup()
    process_trade_candle(row, s, candle(1, 101, 99))  # touches the entry zone, opens the trade
    assert row.status == "open"
    process_trade_candle(row, s, candle(2, 101, 89))  # low breaches the stop exactly
    assert row.status == "closed" and row.exit_reason == "stop_loss"
    assert abs(row.realized_r - D("-1")) < D("0.0000001")


def test_record_exit_fee_and_pnl_formula_are_unchanged():
    """_record_exit() itself was not modified by the sizing fix - it still
    charges execution_fee() and computes pnl() exactly as before."""
    row = trade(initial_quantity=D("0.5"), remaining_quantity=D("0.5"), fee_rate_pct=D("0.1"),
                risk_amount=D("1"), realized_pnl=D("-3"), fees=D("3"))
    _record_exit(row, D("110"), D("0.5"), "heikin_ashi_5m_reversal", NOW)
    expected_fee = execution_fee(D("110"), D("0.5"), D("0.1"))
    expected_pnl_delta = pnl("bullish", D("100"), D("110"), D("0.5"), expected_fee)
    assert row.fees == D("3") + expected_fee
    assert row.realized_pnl == D("-3") + expected_pnl_delta
    assert row.exit_reason == "heikin_ashi_5m_reversal"


def _seeded_setup(db):
    db.add(Symbol(id=1, exchange="binance", symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", market_type="spot"))
    row = TradeSetup(
        id=1, symbol_id=1, direction="bullish", strategy="SMC", status="ready", higher_timeframe="4h",
        setup_timeframe="1h", entry_timeframe="1h", structure_event_id=1, entry_min=99, entry_max=101,
        preferred_entry=100, stop_loss=90, invalidation_price=90, take_profit_1=115, take_profit_2=120,
        take_profit_3=130, confidence_score=75, score_breakdown_json={}, setup_conditions_json={},
        rejection_reasons_json=[], expires_at=NOW + timedelta(hours=4), detected_at=NOW,
    )
    db.add(row)
    db.flush()
    return row


def test_enroll_setup_uses_fee_aware_quantity_and_preserves_risk_amount_one(session_factory):
    with session_factory() as db:
        row = _seeded_setup(db)
        trade_row = enroll_setup(db, row, fee_rate_pct=D("0.1"))
        expected_quantity = _fee_aware_quantity(D("100"), D("90"), D("1"), D("0.1"))
        assert trade_row.initial_quantity == expected_quantity
        assert trade_row.remaining_quantity == expected_quantity
        assert trade_row.risk_amount == D("1")


def test_enrolling_the_same_setup_twice_does_not_resize_the_existing_trade(session_factory):
    """New paper-forward trades only use fee-aware sizing at creation time -
    an already-enrolled trade is never retroactively resized."""
    with session_factory() as db:
        row = _seeded_setup(db)
        first = enroll_setup(db, row, fee_rate_pct=D("0.1"))
        db.flush()
        first_quantity = first.initial_quantity
        second = enroll_setup(db, row, fee_rate_pct=D("0.5"))  # a different fee rate passed on the second call
        assert second.id == first.id
        assert second.initial_quantity == first_quantity  # unchanged - not resized


def test_enroll_setup_returns_none_for_zero_stop_distance(session_factory):
    with session_factory() as db:
        row = _seeded_setup(db)
        row.stop_loss = row.preferred_entry  # zero distance
        db.flush()
        assert enroll_setup(db, row, fee_rate_pct=D("0.1")) is None
