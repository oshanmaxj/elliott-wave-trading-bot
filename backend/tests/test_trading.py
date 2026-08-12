from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.trading.execution import (
    candle_exit,
    execution_fee,
    position_size,
    risk_guards,
    setup_available,
    slipped_price,
)
from app.trading.metrics import calculate_metrics
from app.trading.validation import validate_geometry

D = Decimal


@pytest.mark.parametrize(
    "direction,zone,entry,stop,targets",
    [
        ("bullish", (D("99"), D("101")), D("100"), D("95"), (D("110"), D("115"), D("120"))),
        ("bearish", (D("99"), D("101")), D("100"), D("105"), (D("90"), D("85"), D("80"))),
    ],
)
def test_valid_directional_geometry_and_rr(direction, zone, entry, stop, targets):
    result = validate_geometry(direction, *zone, entry, stop, targets, stop, D("1.5"))
    assert result.valid
    assert result.risk == D("5")
    assert result.risk_rewards == (D("2"), D("3"), D("4"))


@pytest.mark.parametrize(
    "direction,stop,target",
    [("bullish", D("95"), D("110")), ("bearish", D("105"), D("90"))],
)
def test_single_primary_target_can_satisfy_minimum_rr(direction, stop, target):
    result = validate_geometry(
        direction, D("99"), D("101"), D("100"), stop,
        (target, None, None), stop, D("2"),
    )
    assert result.valid
    assert result.risk_rewards == (D("2"), None, None)
    assert "invalid_rr" not in result.reasons


def test_primary_target_below_minimum_rr_is_rejected():
    result = validate_geometry(
        "bullish", D("99"), D("101"), D("100"), D("95"),
        (D("107"), None, None), D("95"), D("2"),
    )
    assert result.risk_rewards == (D("1.4"), None, None)
    assert "invalid_rr" in result.reasons


def test_multiple_targets_validate_against_primary_target():
    result = validate_geometry(
        "bullish", D("99"), D("101"), D("100"), D("95"),
        (D("110"), D("115"), D("120")), D("95"), D("2"),
    )
    assert result.valid
    assert result.risk_rewards == (D("2"), D("3"), D("4"))


@pytest.mark.parametrize(
    "direction,stop,reason",
    [("bullish", D("102"), "invalid_stop_side"), ("bearish", D("98"), "invalid_stop_side")],
)
def test_incorrect_stop_is_rejected(direction, stop, reason):
    result = validate_geometry(direction, D("99"), D("101"), D("100"), stop, (D("105"), D("110"), D("115")))
    assert reason in result.reasons
    assert "negative_risk" in result.reasons


@pytest.mark.parametrize(
    "direction,target",
    [("bullish", D("99")), ("bearish", D("101"))],
)
def test_incorrect_target_side_is_rejected(direction, target):
    stop = D("95") if direction == "bullish" else D("105")
    result = validate_geometry(direction, D("99"), D("101"), D("100"), stop, (target, None, None))
    assert "invalid_target_side" in result.reasons


def test_entry_zone_target_order_and_invalidation_rejections():
    result = validate_geometry("bullish", D("101"), D("99"), D("100"), D("95"), (D("105"), D("104"), None), D("101"))
    assert {"invalid_entry_zone", "entry_outside_zone", "invalid_target_side", "invalid_invalidation_side"} <= set(result.reasons)


def test_position_sizing_caps_risk_and_rejects_zero_distance():
    risk, quantity = position_size(D("10000"), D("2"), D("100"), D("95"), D("1"))
    assert risk == D("100") and quantity == D("20")
    with pytest.raises(ValueError):
        position_size(D("10000"), D("1"), D("100"), D("100"))


def test_fees_and_adverse_slippage():
    assert execution_fee(D("100"), D("2"), D("0.05")) == D("0.10")
    assert slipped_price(D("100"), "bullish", D("10"), True) == D("100.1")
    assert slipped_price(D("100"), "bearish", D("10"), True) == D("99.9")


@pytest.mark.parametrize(
    "direction,high,low,stop,target,reason",
    [
        ("bullish", D("104"), D("94"), D("95"), D("110"), "stop_loss"),
        ("bearish", D("106"), D("96"), D("105"), D("90"), "stop_loss"),
        ("bullish", D("111"), D("99"), D("95"), D("110"), "take_profit"),
        ("bearish", D("101"), D("89"), D("105"), D("90"), "take_profit"),
    ],
)
def test_long_short_stop_and_target_execution(direction, high, low, stop, target, reason):
    assert candle_exit(direction, high, low, stop, target).reason == reason


def test_same_candle_policy_is_deterministic():
    assert candle_exit("bullish", D("111"), D("94"), D("95"), D("110")).reason == "stop_loss"
    assert candle_exit("bullish", D("111"), D("94"), D("95"), D("110"), "target_first").reason == "take_profit"
    assert candle_exit("bullish", D("111"), D("94"), D("95"), D("110"), "skip_ambiguous").price is None


def test_daily_loss_drawdown_and_open_position_guards():
    assert risk_guards(D("9000"), D("10000"), D("-400"), D("10000"), 3, 3, D("3"), D("8")) == [
        "max_open_positions", "daily_loss_guard", "drawdown_guard"
    ]


def test_no_look_ahead_boundary():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    setup = SimpleNamespace(detected_at=now + timedelta(hours=1), expires_at=now + timedelta(days=1))
    candle = SimpleNamespace(open_time=now, close_time=now + timedelta(minutes=59))
    assert not setup_available(setup, candle)


def test_performance_metrics_are_reproducible():
    first = calculate_metrics([D("100"), D("-50"), D("25")], [D("1"), D("-.5"), D(".25")], D("10000"))
    second = calculate_metrics([D("100"), D("-50"), D("25")], [D("1"), D("-.5"), D(".25")], D("10000"))
    assert first == second
    assert first["net_profit"] == D("75")
    assert first["profit_factor"] == D("2.5")
