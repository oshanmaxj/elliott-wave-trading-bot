from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.api.execution import position_overlays
from app.models import LivePosition, Symbol, TradeSetup


def add_setup(
    db, symbol, setup_id_offset, *, direction, timeframe, entry, stop, target
):
    now = datetime.now(timezone.utc)
    setup = TradeSetup(
        symbol_id=symbol.id,
        direction=direction,
        strategy=f"{direction}_continuation",
        status="acknowledged" if setup_id_offset else "triggered",
        higher_timeframe=timeframe,
        setup_timeframe=timeframe,
        entry_timeframe=timeframe,
        structure_event_id=setup_id_offset + 1,
        entry_min=entry,
        entry_max=entry,
        preferred_entry=entry,
        stop_loss=stop,
        invalidation_price=stop,
        take_profit_1=target,
        risk_reward_1=Decimal("2"),
        confidence_score=Decimal("80"),
        expires_at=now + timedelta(hours=1),
        detected_at=now,
    )
    db.add(setup)
    db.flush()
    return setup


def test_active_overlays_use_originating_setups_not_latest_analysis(session_factory):
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
        active_long = add_setup(
            db,
            symbol,
            1,
            direction="bullish",
            timeframe="1m",
            entry=Decimal("100"),
            stop=Decimal("95"),
            target=Decimal("110"),
        )
        latest_bearish = add_setup(
            db,
            symbol,
            2,
            direction="bearish",
            timeframe="1h",
            entry=Decimal("120"),
            stop=Decimal("130"),
            target=Decimal("100"),
        )
        second_long = add_setup(
            db,
            symbol,
            3,
            direction="bullish",
            timeframe="5m",
            entry=Decimal("200"),
            stop=Decimal("190"),
            target=Decimal("220"),
        )
        db.add_all(
            [
                LivePosition(
                    environment="testnet",
                    symbol_id=symbol.id,
                    originating_trade_setup_id=active_long.id,
                    direction="long",
                    status="open",
                    base_quantity=Decimal("1"),
                    remaining_quantity=Decimal("1"),
                    average_entry=Decimal("101"),
                    stop_loss=Decimal("95"),
                    take_profit_1=Decimal("110"),
                    protection_status="unprotected",
                    opened_at=now,
                ),
                LivePosition(
                    environment="testnet",
                    symbol_id=symbol.id,
                    originating_trade_setup_id=second_long.id,
                    direction="long",
                    status="open",
                    base_quantity=Decimal("1"),
                    remaining_quantity=Decimal("1"),
                    average_entry=Decimal("201"),
                    stop_loss=Decimal("190"),
                    take_profit_1=Decimal("220"),
                    protection_status="protected",
                    opened_at=now + timedelta(minutes=1),
                ),
            ]
        )
        latest_id = latest_bearish.id
        active_id = active_long.id
    with session_factory() as db:
        rows = position_overlays("BTCUSDT", db)
    assert [row["setup_id"] for row in rows] != [latest_id]
    assert rows[0]["setup_id"] == active_id
    assert rows[0]["direction"] == "bullish"
    assert rows[0]["entry"] == Decimal("101")
    assert rows[0]["stop_loss"] == Decimal("95")
    assert rows[0]["protection_status"] == "unprotected"
    assert len(rows) == 2


def test_closed_positions_are_absent_and_live_position_geometry_is_canonical(session_factory):
    now = datetime.now(timezone.utc)
    with session_factory.begin() as db:
        symbol = Symbol(exchange="binance", symbol="BTCUSDT", base_asset="BTC",
            quote_asset="USDT", market_type="spot")
        db.add(symbol)
        db.flush()
        setup = add_setup(db, symbol, 10, direction="bullish", timeframe="1h",
            entry=Decimal("100"), stop=Decimal("1"), target=Decimal("999"))
        common = dict(environment="testnet", symbol_id=symbol.id,
            originating_trade_setup_id=setup.id, direction="long", base_quantity=Decimal("1"),
            remaining_quantity=Decimal("1"), average_entry=Decimal("101"), stop_loss=Decimal("95"),
            take_profit_1=Decimal("110"), protection_status="protected", opened_at=now)
        db.add_all([LivePosition(**common, status="open"),
                    LivePosition(**common, status="closed", closed_at=now)])
    with session_factory() as db:
        rows = position_overlays("BTCUSDT", db)
    assert len(rows) == 1
    assert rows[0]["canonical_active"] is True
    assert rows[0]["stop_loss"] == Decimal("95")
    assert rows[0]["take_profit_1"] == Decimal("110")
