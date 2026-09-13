"""Tests for the isolated elliott_wave3_heikin_ashi strategy.

Covers: strategy generation gating (old strategies vs the new one), causal
15m Elliott + 5m structure + 1m Heikin Ashi entry detection, duplicate/
re-entry protection, no-look-ahead, and the three paper-forward exit paths
(hard stop, Elliott invalidation, 5m opposite HA reversal). Heikin Ashi
values are asserted to never leak into execution prices.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.bot import STRATEGIES
from app.execution.strategies import SETUP_TO_RUNTIME_STRATEGY, runtime_strategy_for_setup_name
from app.models import (
    BotLog, BotRuntimeState, Candle, ElliottWaveCount, ElliottWavePoint, MarketStructureEvent,
    PaperForwardTrade, SwingPoint, Symbol, TradeSetup,
)
from app.repositories.market import ensure_symbol, upsert_candle
from app.schemas.common import CandleData
from app.services.pipeline import process_closed_candle, strategy_generation_allowed
from app.strategies import elliott_wave3_heikin_ashi as wave3_ha
from app.trading.paper_forward import (
    WAVE3_HA_STRATEGY, _wave3_ha_1m_update, _wave3_ha_5m_exit, setup_is_eligible,
)

D = Decimal
START = datetime(2025, 1, 1, tzinfo=timezone.utc)
STEP = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1h": timedelta(hours=1)}
# The HA wick/body and body/ATR filters are already covered by
# test_wave3_heikin_ashi.py and test_heikin_ashi_trend_break.py; the fixtures
# here are built to exercise the Elliott-gate/structure/dedup/exit logic, so
# they use lenient HA thresholds (mirroring the existing tests' own pattern)
# to isolate that logic from the reversal-shape filters.
LENIENT_HA = {"elliott_wave3_heikin_ashi": {"ha_wick_body_max_ratio": "10", "ha_body_atr_min_ratio": "0.01"}}

BULLISH_1M_ROWS = [(100, 101, 98, 99)] * 17 + [(99, 100, 96, 97), (97, 98, 94, 95), (95, 101, 95, 100), (100, 103, 99, 102)]
BEARISH_1M_ROWS = [(100, 102, 99, 101)] * 17 + [(101, 104, 101, 103), (103, 106, 103, 105), (105, 105, 99, 100), (100, 101, 97, 98)]


def _symbol(db):
    row = Symbol(exchange="binance", symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", market_type="spot")
    db.add(row)
    db.flush()
    return row


def _add_candles(db, symbol_id, timeframe, rows, start=START):
    step = STEP[timeframe]
    created = []
    for index, (o, h, l, c) in enumerate(rows):
        opened = start + step * index
        candle = Candle(
            symbol_id=symbol_id, timeframe=timeframe, open_time=opened,
            close_time=opened + step - timedelta(milliseconds=1),
            open=D(str(o)), high=D(str(h)), low=D(str(l)), close=D(str(c)),
            volume=D("100"), quote_volume=D("100"), trade_count=10,
            taker_buy_base_volume=D("50"), taker_buy_quote_volume=D("50"), is_closed=True,
        )
        db.add(candle)
        created.append(candle)
    db.flush()
    return created


def _seed_wave3_context(db, symbol, direction, decision_time, invalidation_price=None):
    """One 15m ElliottWaveCount with Wave 0/1/2 points, plus a 5m BOS in the same direction."""
    anchor_candles = _add_candles(db, symbol.id, "15m", [(100, 101, 99, 100)] * 3, start=START - timedelta(days=1))
    if direction == "bullish":
        prices, swing_types = (90, 110, 97), ("low", "high", "low")
        default_invalidation = D("92")
    else:
        prices, swing_types = (110, 90, 103), ("high", "low", "high")
        default_invalidation = D("108")
    swings = []
    for candle, price, swing_type in zip(anchor_candles, prices, swing_types):
        swing = SwingPoint(symbol_id=symbol.id, timeframe="15m", candle_id=candle.id, swing_type=swing_type,
                            price=D(str(price)), strength=D("0.8"), confirmation_candles=3, detected_at=candle.close_time)
        db.add(swing)
        db.flush()
        swings.append(swing)
    count = ElliottWaveCount(
        symbol_id=symbol.id, timeframe="15m", degree="minor", direction=direction, pattern_type=f"{direction}_impulse",
        status="primary", rank=1, confidence_score=D("82"), start_candle_id=anchor_candles[0].id, end_candle_id=anchor_candles[-1].id,
        invalidation_price=invalidation_price if invalidation_price is not None else default_invalidation,
        metadata_json={"current_wave": "2", "phase": "wave_2_complete"},
        detected_at=START - timedelta(hours=1), confirmed_at=START - timedelta(hours=1),
    )
    db.add(count)
    db.flush()
    for index, (swing, price) in enumerate(zip(swings, prices)):
        db.add(ElliottWavePoint(wave_count_id=count.id, wave_label=str(index), sequence_number=index,
                                 swing_point_id=swing.id, candle_id=swing.candle_id, price=D(str(price)),
                                 timestamp=anchor_candles[index].open_time))
    db.flush()
    structure_candle = _add_candles(db, symbol.id, "5m", [(100, 101, 99, 100)], start=START - timedelta(hours=2))[0]
    structure_swing = SwingPoint(symbol_id=symbol.id, timeframe="5m", candle_id=structure_candle.id,
                                  swing_type="low" if direction == "bullish" else "high", price=D("95"),
                                  strength=D("0.8"), confirmation_candles=3, detected_at=structure_candle.close_time)
    db.add(structure_swing)
    db.flush()
    event = MarketStructureEvent(
        symbol_id=symbol.id, timeframe="5m", event_type="BOS", direction=direction,
        broken_swing_id=structure_swing.id, confirmation_candle_id=structure_candle.id,
        break_price=D("100"), previous_trend="ranging", resulting_trend=direction,
        confidence=D("80"), detected_at=structure_candle.close_time,
    )
    db.add(event)
    db.flush()
    assert decision_time >= event.detected_at and decision_time >= count.detected_at
    return count, event


def test_strategy_registered_in_runtime_mapping_and_bot_strategies_allowlist():
    assert SETUP_TO_RUNTIME_STRATEGY["elliott_wave3_heikin_ashi"] == "elliott_wave3_heikin_ashi"
    assert runtime_strategy_for_setup_name("elliott_wave3_heikin_ashi") == "elliott_wave3_heikin_ashi"
    assert "elliott_wave3_heikin_ashi" in STRATEGIES


def test_strategy_generation_allowed_defaults_unrestricted_and_can_isolate_the_new_strategy():
    assert strategy_generation_allowed(None, "bos_continuation") is True
    assert strategy_generation_allowed(SimpleNamespace(enabled_strategies_json=[]), "bos_continuation") is True
    restricted = SimpleNamespace(enabled_strategies_json=["elliott_wave3_heikin_ashi"])
    assert strategy_generation_allowed(restricted, "elliott_wave3_heikin_ashi") is True
    assert strategy_generation_allowed(restricted, "bos_continuation") is False
    assert strategy_generation_allowed(restricted, "wave_3_continuation") is False
    assert strategy_generation_allowed(restricted, None) is False


def test_disabling_old_strategies_does_not_touch_historical_rows(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        structure_candle = _add_candles(db, symbol.id, "1h", [(100, 101, 99, 100)])[0]
        swing = SwingPoint(symbol_id=symbol.id, timeframe="1h", candle_id=structure_candle.id, swing_type="low",
                            price=D("95"), strength=D("0.8"), confirmation_candles=3, detected_at=structure_candle.close_time)
        db.add(swing); db.flush()
        event = MarketStructureEvent(symbol_id=symbol.id, timeframe="1h", event_type="BOS", direction="bullish",
                                      broken_swing_id=swing.id, confirmation_candle_id=structure_candle.id, break_price=D("100"),
                                      previous_trend="ranging", resulting_trend="bullish", confidence=D("80"), detected_at=structure_candle.close_time)
        db.add(event); db.flush()
        old_setup = TradeSetup(symbol_id=symbol.id, direction="bullish", strategy="bullish_continuation", status="ready",
                                higher_timeframe="1h", setup_timeframe="1h", entry_timeframe="1h", structure_event_id=event.id,
                                preferred_entry=D("100"), stop_loss=D("95"), confidence_score=D("80"), expires_at=structure_candle.close_time + timedelta(days=1),
                                detected_at=structure_candle.close_time)
        db.add(old_setup); db.commit()
        old_setup_id = old_setup.id
        restricted = SimpleNamespace(enabled_strategies_json=["elliott_wave3_heikin_ashi"])
        assert strategy_generation_allowed(restricted, runtime_strategy_for_setup_name("bullish_continuation")) is False
        reloaded = db.get(TradeSetup, old_setup_id)
        assert reloaded is not None and reloaded.strategy == "bullish_continuation" and reloaded.status == "ready"


def test_valid_long_entry_uses_only_real_prices_and_persists_wave_audit_fields(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        candles = _add_candles(db, symbol.id, "1m", BULLISH_1M_ROWS)
        decision_time = candles[-1].close_time
        count, event = _seed_wave3_context(db, symbol, "bullish", decision_time)
        db.commit()
        decision = wave3_ha.evaluate_entry(db, candles[-1], wave3_ha.load_config(LENIENT_HA))
        assert decision is not None
        assert decision.direction == "bullish"
        assert decision.entry == D("102")  # real close of the confirmation candle
        assert decision.entry != D("101")  # sanity: not the HA close (would differ)
        assert decision.stop < decision.entry
        assert decision.elliott_wave_count_id == count.id
        assert decision.structure_event_id == event.id
        assert D(decision.conditions["wave1_price"]) == D("110")
        assert D(decision.conditions["wave2_price"]) == D("97")
        assert decision.conditions["expected_wave3_direction"] == "bullish"
        assert decision.conditions["entry_timeframe"] == "1m"
        assert decision.conditions["exit_timeframe"] == "5m"


def test_valid_short_entry_mirrors_long(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        candles = _add_candles(db, symbol.id, "1m", BEARISH_1M_ROWS)
        decision_time = candles[-1].close_time
        # Captured (not just called) so the ORM identity map can't drop this
        # row via a weakref GC before evaluate_entry re-queries it - SQLite
        # (test-only; production is Postgres) loses tzinfo on a cold reload.
        kept_alive = _seed_wave3_context(db, symbol, "bearish", decision_time)
        db.commit()
        decision = wave3_ha.evaluate_entry(db, candles[-1], wave3_ha.load_config(LENIENT_HA))
        assert decision is not None
        assert decision.direction == "bearish"
        assert decision.entry == D("98")
        assert decision.stop > decision.entry


def test_wave2_invalidation_rejects_the_entry(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        candles = _add_candles(db, symbol.id, "1m", BULLISH_1M_ROWS)
        decision_time = candles[-1].close_time
        # invalidation_price above the real entry price: Wave 2 has already broken.
        kept_alive = _seed_wave3_context(db, symbol, "bullish", decision_time, invalidation_price=D("150"))
        db.commit()
        assert wave3_ha.evaluate_entry(db, candles[-1], wave3_ha.load_config(LENIENT_HA)) is None


def test_missing_5m_structure_confirmation_rejects_the_entry(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        candles = _add_candles(db, symbol.id, "1m", BULLISH_1M_ROWS)
        anchor_candles = _add_candles(db, symbol.id, "15m", [(100, 101, 99, 100)] * 3, start=START - timedelta(days=1))
        prices = (90, 110, 97)
        swings = []
        for candle, price, swing_type in zip(anchor_candles, prices, ("low", "high", "low")):
            swing = SwingPoint(symbol_id=symbol.id, timeframe="15m", candle_id=candle.id, swing_type=swing_type,
                                price=D(str(price)), strength=D("0.8"), confirmation_candles=3, detected_at=candle.close_time)
            db.add(swing); db.flush(); swings.append(swing)
        count = ElliottWaveCount(symbol_id=symbol.id, timeframe="15m", degree="minor", direction="bullish", pattern_type="bullish_impulse",
                                  status="primary", rank=1, confidence_score=D("82"), start_candle_id=anchor_candles[0].id, end_candle_id=anchor_candles[-1].id,
                                  invalidation_price=D("92"), metadata_json={"current_wave": "2", "phase": "wave_2_complete"},
                                  detected_at=START - timedelta(hours=1))
        db.add(count); db.flush()
        for index, (swing, price) in enumerate(zip(swings, prices)):
            db.add(ElliottWavePoint(wave_count_id=count.id, wave_label=str(index), sequence_number=index,
                                     swing_point_id=swing.id, candle_id=swing.candle_id, price=D(str(price)), timestamp=anchor_candles[index].open_time))
        db.commit()
        # No 5m MarketStructureEvent seeded - the Elliott context alone must never be enough.
        assert wave3_ha.evaluate_entry(db, candles[-1], wave3_ha.load_config(LENIENT_HA)) is None


def test_duplicate_signal_prevention_and_configurable_reentry(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        candles = _add_candles(db, symbol.id, "1m", BULLISH_1M_ROWS)
        decision_time = candles[-1].close_time
        kept_alive = _seed_wave3_context(db, symbol, "bullish", decision_time)
        db.commit()
        config = wave3_ha.load_config(LENIENT_HA)
        first = wave3_ha.evaluate_entry(db, candles[-1], config)
        assert first is not None
        setup = TradeSetup(symbol_id=symbol.id, direction=first.direction, strategy=wave3_ha.STRATEGY, status="ready",
                            higher_timeframe="15m", setup_timeframe="1m", entry_timeframe="1m", structure_event_id=first.structure_event_id,
                            elliott_wave_count_id=first.elliott_wave_count_id, entry_min=first.entry_min, entry_max=first.entry_max,
                            preferred_entry=first.entry, stop_loss=first.stop, confidence_score=first.confidence_score,
                            setup_conditions_json=first.conditions, expires_at=decision_time + timedelta(minutes=15), detected_at=decision_time)
        db.add(setup); db.commit()
        assert wave3_ha.evaluate_entry(db, candles[-1], config) is None  # same reversal event, reentry disabled by default
        reentry_config = wave3_ha.load_config({"elliott_wave3_heikin_ashi": {**LENIENT_HA["elliott_wave3_heikin_ashi"], "reentry_enabled": True}})
        second = wave3_ha.evaluate_entry(db, candles[-1], reentry_config)
        assert second is not None and second.conditions["event_fingerprint"] == first.conditions["event_fingerprint"]


def test_no_look_ahead_a_later_candle_cannot_change_an_earlier_decision(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        candles = _add_candles(db, symbol.id, "1m", BULLISH_1M_ROWS)
        decision_time = candles[-1].close_time
        kept_alive = _seed_wave3_context(db, symbol, "bullish", decision_time)
        db.commit()
        config = wave3_ha.load_config(LENIENT_HA)
        first = wave3_ha.evaluate_entry(db, candles[-1], config)
        assert first is not None
        # A dramatic future candle is appended after the decision candle.
        _add_candles(db, symbol.id, "1m", [(100, 999, 1, 500)], start=candles[-1].close_time + timedelta(milliseconds=1))
        db.commit()
        second = wave3_ha.evaluate_entry(db, candles[-1], config)
        assert second is not None
        assert (second.direction, second.entry, second.stop, second.conditions["event_fingerprint"]) == \
               (first.direction, first.entry, first.stop, first.conditions["event_fingerprint"])


def test_wave3_still_intact_uses_fixed_invalidation_price_not_mutated_state():
    bullish = SimpleNamespace(direction="bullish", invalidation_price=D("90"))
    assert wave3_ha.wave3_still_intact(bullish, D("95")) is True
    assert wave3_ha.wave3_still_intact(bullish, D("89")) is False
    bearish = SimpleNamespace(direction="bearish", invalidation_price=D("110"))
    assert wave3_ha.wave3_still_intact(bearish, D("105")) is True
    assert wave3_ha.wave3_still_intact(bearish, D("111")) is False
    assert wave3_ha.wave3_still_intact(None, D("100")) is True


def _open_trade(symbol_id, direction, entry, stop):
    return PaperForwardTrade(setup_id=1, symbol_id=symbol_id, symbol="BTCUSDT", strategy=WAVE3_HA_STRATEGY,
                              direction=direction, timeframe="1m", confidence_score=D("80"), simulated_entry=entry,
                              entry_min=entry, entry_max=entry, stop_loss=stop, active_stop=stop, initial_quantity=D("1"),
                              remaining_quantity=D("1"), risk_amount=D(str(abs(entry - stop))), status="open", opened_at=START)


def test_hard_stop_wins_before_elliott_invalidation_on_the_same_candle(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        count = ElliottWaveCount(symbol_id=symbol.id, timeframe="15m", degree="minor", direction="bullish", pattern_type="bullish_impulse",
                                  status="primary", rank=1, confidence_score=D("82"), start_candle_id=1, end_candle_id=1,
                                  invalidation_price=D("95"), metadata_json={}, detected_at=START)
        candle = _add_candles(db, symbol.id, "15m", [(100, 101, 99, 100)])[0]
        count.start_candle_id = count.end_candle_id = candle.id
        db.add(count); db.flush()
        setup = TradeSetup(symbol_id=symbol.id, direction="bullish", strategy=WAVE3_HA_STRATEGY, status="ready",
                            higher_timeframe="15m", setup_timeframe="1m", entry_timeframe="1m", structure_event_id=1,
                            elliott_wave_count_id=count.id, preferred_entry=D("102"), stop_loss=D("99"),
                            confidence_score=D("80"), expires_at=START + timedelta(days=1), detected_at=START)
        db.add(setup); db.flush()
        trade = _open_trade(symbol.id, "bullish", D("102"), D("99"))
        trade.setup_id = setup.id
        db.add(trade); db.flush()
        one_min = _add_candles(db, symbol.id, "1m", [(100, 101, 90, 94)])[0]  # low breaches stop AND close breaches invalidation
        _wave3_ha_1m_update(db, trade, setup, one_min)
        assert trade.exit_reason == wave3_ha.EXIT_REASON_HARD_STOP
        assert trade.status == "closed"


def test_elliott_invalidation_exit_when_hard_stop_not_touched(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        candle15 = _add_candles(db, symbol.id, "15m", [(100, 101, 99, 100)])[0]
        count = ElliottWaveCount(symbol_id=symbol.id, timeframe="15m", degree="minor", direction="bullish", pattern_type="bullish_impulse",
                                  status="primary", rank=1, confidence_score=D("82"), start_candle_id=candle15.id, end_candle_id=candle15.id,
                                  invalidation_price=D("95"), metadata_json={}, detected_at=START)
        db.add(count); db.flush()
        setup = TradeSetup(symbol_id=symbol.id, direction="bullish", strategy=WAVE3_HA_STRATEGY, status="ready",
                            higher_timeframe="15m", setup_timeframe="1m", entry_timeframe="1m", structure_event_id=1,
                            elliott_wave_count_id=count.id, preferred_entry=D("102"), stop_loss=D("90"),
                            confidence_score=D("80"), expires_at=START + timedelta(days=1), detected_at=START)
        db.add(setup); db.flush()
        trade = _open_trade(symbol.id, "bullish", D("102"), D("90"))
        trade.setup_id = setup.id
        db.add(trade); db.flush()
        one_min = _add_candles(db, symbol.id, "1m", [(100, 101, 93, 94)])[0]  # low does not touch stop(90); close(94) breaks invalidation(95)
        _wave3_ha_1m_update(db, trade, setup, one_min)
        assert trade.exit_reason == wave3_ha.EXIT_REASON_INVALIDATED
        assert trade.status == "closed"


def test_5m_opposite_heikin_ashi_reversal_exits_a_bullish_trade(session_factory):
    with session_factory() as db:
        symbol = _symbol(db)
        setup = TradeSetup(symbol_id=symbol.id, direction="bullish", strategy=WAVE3_HA_STRATEGY, status="ready",
                            higher_timeframe="15m", setup_timeframe="1m", entry_timeframe="1m", structure_event_id=1,
                            preferred_entry=D("102"), stop_loss=D("90"), confidence_score=D("80"),
                            expires_at=START + timedelta(days=1), detected_at=START)
        db.add(setup); db.flush()
        trade = _open_trade(symbol.id, "bullish", D("102"), D("90"))
        trade.setup_id = setup.id
        db.add(trade); db.flush()
        # A bearish 5m Heikin Ashi reversal: neutral base, two bullish pullback candles, then a confirmed bearish reversal.
        candles = _add_candles(db, symbol.id, "5m", BEARISH_1M_ROWS)
        exited = _wave3_ha_5m_exit(db, trade, candles[-1], wave3_ha.load_config(LENIENT_HA))
        assert exited is True
        assert trade.exit_reason == wave3_ha.EXIT_REASON_HA_REVERSAL
        assert trade.exit_signal_candle_id == candles[-1].id
        assert trade.status == "closed"


def test_setup_is_eligible_allows_wave3_ha_without_a_take_profit_ladder():
    base = dict(symbol_id=1, direction="bullish", status="ready", higher_timeframe="15m", setup_timeframe="1m",
                entry_timeframe="1m", structure_event_id=1, preferred_entry=D("100"), entry_min=D("99.9"),
                entry_max=D("100.1"), stop_loss=D("95"), confidence_score=D("80"),
                expires_at=START + timedelta(days=1), detected_at=START)
    wave3_setup = TradeSetup(strategy=WAVE3_HA_STRATEGY, **base)
    other_setup = TradeSetup(strategy="bullish_continuation", **base)
    assert setup_is_eligible(wave3_setup) is True
    assert setup_is_eligible(other_setup) is False  # generic strategies still require at least one TP


def test_multi_timeframe_roles_are_15m_context_5m_structure_and_exit_1m_entry():
    assert wave3_ha.ELLIOTT_CONTEXT_TIMEFRAME == "15m"
    assert wave3_ha.STRUCTURE_TIMEFRAME == "5m"
    assert wave3_ha.ENTRY_TIMEFRAME == "1m"
    assert wave3_ha.EXIT_TIMEFRAME == "5m"


def test_config_defaults_keep_auto_execution_off_and_paper_enabled():
    config = wave3_ha.load_config({})
    assert config.paper_enabled is True
    assert config.auto_execution_enabled is False
    assert config.reentry_enabled is False


@pytest.mark.asyncio
async def test_restricting_enabled_strategies_blocks_old_strategy_generation_end_to_end(session_factory):
    """Full process_closed_candle integration proof for safety condition A.

    Reuses the exact 1h price sequence from
    test_lower_timeframes.py::test_fresh_closed_candles_can_persist_every_analysis_stage,
    which is proven elsewhere to persist >=1 TradeSetup when generation is
    unrestricted. Here the same sequence runs with enabled_strategies_json
    restricted to elliott_wave3_heikin_ashi only, and must persist zero
    TradeSetup rows from any other strategy while structure/swing/FVG/order
    block detection still proceeds normally (proving the gate is specific
    to setup creation, not a side effect of blocking the whole pipeline).
    """
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
    step = timedelta(hours=1)
    candle_ids = []
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, "BTCUSDT")
        db.add(BotRuntimeState(enabled_strategies_json=["elliott_wave3_heikin_ashi"]))
        for index, (high, low, open_price, close) in enumerate(prices):
            data = CandleData(
                open_time=opened + index * step, close_time=opened + (index + 1) * step - timedelta(milliseconds=1),
                open=D(str(open_price)), high=D(str(high)), low=D(str(low)), close=D(str(close)),
                volume=D("10"), quote_volume=D("1000"), trade_count=10,
                taker_buy_base_volume=D("5"), taker_buy_quote_volume=D("500"), is_closed=True,
            )
            row, _ = upsert_candle(db, symbol.id, "1h", data)
            candle_ids.append(row.id)
    for candle_id in candle_ids:
        await process_closed_candle(candle_id, broadcast=False, session_factory=session_factory)
    with session_factory() as db:
        assert db.query(SwingPoint).count() >= 2  # detection pipeline still runs normally
        assert db.query(MarketStructureEvent).count() >= 1
        other_strategy_setups = db.query(TradeSetup).filter(TradeSetup.strategy != "elliott_wave3_heikin_ashi").count()
        assert other_strategy_setups == 0


def test_preflight_blocks_both_manual_and_automatic_execution_even_when_strategy_enabled(session_factory):
    """Safety condition B, hard-coded independent of runtime configuration.

    Even in the exact configuration the strategy needs to run at all
    (enabled_strategies_json containing only elliott_wave3_heikin_ashi, which
    would otherwise satisfy every other execution precondition), execution
    must still be refused for both the manual-approved and automatic paths.
    """
    from app.execution.orchestrator import AutomaticTestnetExecutor

    with session_factory.begin() as db:
        symbol = _symbol(db)
        db.add(BotRuntimeState(
            status="running", automatic_trading_enabled=True, manual_approval_required=False,
            pause_new_entries=False, kill_switch_enabled=False,
            enabled_symbols_json=["BTCUSDT"], enabled_timeframes_json=["1m"],
            enabled_strategies_json=[wave3_ha.STRATEGY],
        ))
        setup = TradeSetup(symbol_id=symbol.id, direction="bullish", strategy=wave3_ha.STRATEGY, status="ready",
                            higher_timeframe="15m", setup_timeframe="1m", entry_timeframe="1m", structure_event_id=1,
                            preferred_entry=D("100"), entry_min=D("99.9"), entry_max=D("100.1"), stop_loss=D("95"),
                            confidence_score=D("90"), expires_at=START + timedelta(days=1), detected_at=START)
        db.add(setup)
        db.flush()
        symbol_id, setup_id = symbol.id, setup.id
    with session_factory() as db:
        symbol = db.get(Symbol, symbol_id)
        setup = db.get(TradeSetup, setup_id)
        executor = AutomaticTestnetExecutor()
        automatic_reasons = executor._preflight_reasons(db, setup, symbol, manual_approved=False)
        manual_reasons = executor._preflight_reasons(db, setup, symbol, manual_approved=True)
    assert "elliott_wave3_heikin_ashi_is_paper_only" in automatic_reasons
    assert "elliott_wave3_heikin_ashi_is_paper_only" in manual_reasons


@pytest.mark.asyncio
async def test_lifecycle_trigger_never_auto_routes_a_wave3_ha_setup(session_factory):
    """The pre-existing, strategy-agnostic lifecycle-trigger path (used by every
    strategy once a TradeSetup reaches 'triggered') must not be able to route
    elliott_wave3_heikin_ashi to automatic execution either, even when global
    automation is fully enabled.
    """
    opened = datetime(2026, 8, 1, tzinfo=timezone.utc)
    step = timedelta(minutes=1)
    with session_factory.begin() as db:
        symbol = ensure_symbol(db, "BTCUSDT")
        db.add(BotRuntimeState(
            status="running", automatic_trading_enabled=True, manual_approval_required=False,
            pause_new_entries=False, kill_switch_enabled=False,
            enabled_symbols_json=["BTCUSDT"], enabled_timeframes_json=["1m"],
            enabled_strategies_json=[wave3_ha.STRATEGY],
        ))
        setup = TradeSetup(symbol_id=symbol.id, direction="bullish", strategy=wave3_ha.STRATEGY, status="ready",
                            higher_timeframe="15m", setup_timeframe="1m", entry_timeframe="1m", structure_event_id=1,
                            preferred_entry=D("100"), entry_min=D("99.9"), entry_max=D("100.1"), stop_loss=D("95"),
                            confidence_score=D("90"), expires_at=opened + timedelta(days=1), detected_at=opened)
        db.add(setup)
        # A closed 1m candle whose range touches the entry zone, which is
        # exactly what flips a 'ready' setup to 'triggered' in the generic
        # lifecycle loop shared by every strategy.
        data = CandleData(
            open_time=opened, close_time=opened + step - timedelta(milliseconds=1),
            open=D("100"), high=D("100.2"), low=D("99.8"), close=D("100"),
            volume=D("10"), quote_volume=D("1000"), trade_count=10,
            taker_buy_base_volume=D("5"), taker_buy_quote_volume=D("500"), is_closed=True,
        )
        candle, _ = upsert_candle(db, symbol.id, "1m", data)
        candle_id = candle.id
        db.flush()
        setup_id = setup.id
    result = await process_closed_candle(candle_id, broadcast=False, session_factory=session_factory)
    assert result["processed"] is True
    with session_factory() as db:
        reloaded = db.get(TradeSetup, setup_id)
        assert reloaded.status == "triggered"  # lifecycle status itself is unaffected
        eligible_logs = [
            log for log in db.query(BotLog).filter(BotLog.event_type == "execution_eligible").all()
            if log.context_json.get("strategy") == wave3_ha.STRATEGY
        ]
        assert eligible_logs == []
