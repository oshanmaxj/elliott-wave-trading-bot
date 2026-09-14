"""Live/paper strategy: 15m Elliott Wave-3 context + 1m Heikin Ashi entry + 5m Heikin Ashi exit.

This is deliberately separate from `elliott_wave3_heikin_ashi_reversal` (the
existing research-only audit-signal generator, which writes
`Wave3HAResearchSignal` rows and is untouched by this module). This module
creates real, paper-tradeable `TradeSetup` rows so the strategy can flow
through the platform's existing paper-forward simulation, approval queue and
Strategy Performance reporting.

Heikin Ashi values are signal data only. Every price returned from this
module (`EntryDecision.entry`, `.stop`) is a REAL market price taken from
closed real candles - never a synthetic HA open/high/low/close. Only closed
candles are read, and every lookup is bounded by `<= decision_time`, so a
signal at time T cannot be influenced by any candle that closes after T.
"""

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import select

from app.models import Candle, ElliottWaveCount, MarketStructureEvent, SwingPoint, TradeSetup
from app.strategies.elliott_wave3_heikin_ashi_reversal import structural_stop, wave3_gate
from app.strategies.heikin_ashi import atr_at, confirmed_reversal, derive_heikin_ashi

D = Decimal
STRATEGY = "elliott_wave3_heikin_ashi"
ELLIOTT_CONTEXT_TIMEFRAME = "15m"
STRUCTURE_TIMEFRAME = "5m"
ENTRY_TIMEFRAME = "1m"
EXIT_TIMEFRAME = "5m"
EXIT_REASON_HARD_STOP = "hard_stop_loss"
EXIT_REASON_INVALIDATED = "elliott_wave3_invalidated"
EXIT_REASON_HA_REVERSAL = "heikin_ashi_5m_reversal"

DEFAULT_CONFIG = {
    "enabled": True,
    "paper_enabled": True,
    "auto_execution_enabled": False,
    "ha_pullback_min_candles": 2,
    "ha_wick_body_max_ratio": "0.20",
    "ha_body_atr_min_ratio": "0.25",
    "ha_confirmation_required": True,
    "elliott_context_timeframe": ELLIOTT_CONTEXT_TIMEFRAME,
    "structure_timeframe": STRUCTURE_TIMEFRAME,
    "entry_timeframe": ENTRY_TIMEFRAME,
    "exit_timeframe": EXIT_TIMEFRAME,
    "atr_stop_buffer": "0.25",
    "max_stop_atr_ratio": "3",
    "minimum_confidence": "70",
    "reentry_enabled": False,
}


@dataclass(frozen=True)
class Config:
    enabled: bool
    paper_enabled: bool
    auto_execution_enabled: bool
    ha_pullback_min_candles: int
    ha_wick_body_max_ratio: Decimal
    ha_body_atr_min_ratio: Decimal
    ha_confirmation_required: bool
    atr_stop_buffer: Decimal
    max_stop_atr_ratio: Decimal
    minimum_confidence: Decimal
    reentry_enabled: bool


def load_config(strategy_config_json: dict[str, Any] | None) -> Config:
    raw = {**DEFAULT_CONFIG, **((strategy_config_json or {}).get(STRATEGY) or {})}
    return Config(
        enabled=bool(raw["enabled"]),
        paper_enabled=bool(raw["paper_enabled"]),
        auto_execution_enabled=bool(raw["auto_execution_enabled"]),
        ha_pullback_min_candles=int(raw["ha_pullback_min_candles"]),
        ha_wick_body_max_ratio=D(str(raw["ha_wick_body_max_ratio"])),
        ha_body_atr_min_ratio=D(str(raw["ha_body_atr_min_ratio"])),
        ha_confirmation_required=bool(raw["ha_confirmation_required"]),
        atr_stop_buffer=D(str(raw["atr_stop_buffer"])),
        max_stop_atr_ratio=D(str(raw.get("max_stop_atr_ratio", "3"))),
        minimum_confidence=D(str(raw["minimum_confidence"])),
        reentry_enabled=bool(raw["reentry_enabled"]),
    )


@dataclass(frozen=True)
class EntryDecision:
    direction: str
    entry: Decimal
    entry_min: Decimal
    entry_max: Decimal
    stop: Decimal
    elliott_wave_count_id: int
    structure_event_id: int
    confidence_score: Decimal
    score_breakdown: dict[str, Any]
    conditions: dict[str, Any]


def event_fingerprint(symbol_id: int, direction: str, count_id: int, reversal_candle_id: int) -> str:
    return sha256(f"{STRATEGY}:{symbol_id}:{direction}:{count_id}:{reversal_candle_id}".encode()).hexdigest()


def _recent_confirmed_swing(db, symbol_id: int, direction: str, decision_time) -> SwingPoint | None:
    swing_type = "low" if direction == "bullish" else "high"
    return db.scalar(
        select(SwingPoint)
        .where(
            SwingPoint.symbol_id == symbol_id,
            SwingPoint.timeframe == STRUCTURE_TIMEFRAME,
            SwingPoint.swing_type == swing_type,
            SwingPoint.detected_at <= decision_time,
        )
        .order_by(SwingPoint.detected_at.desc(), SwingPoint.id.desc())
        .limit(1)
    )


def _already_used(db, symbol_id: int, fingerprint: str) -> bool:
    rows = db.scalars(
        select(TradeSetup.setup_conditions_json)
        .where(TradeSetup.strategy == STRATEGY, TradeSetup.symbol_id == symbol_id)
        .order_by(TradeSetup.id.desc())
        .limit(500)
    )
    return any((row or {}).get("event_fingerprint") == fingerprint for row in rows)


# Ordered from earliest gate to latest. Used only to pick which of the two
# per-direction rejection reasons is most informative when neither direction
# produces a setup - it has no effect on the entry decision itself.
_REJECTION_GATE_ORDER = [
    "no_ha_reversal",
    "no_valid_wave3_context",
    "wave3_gate_failed",
    "no_structure_event",
    "insufficient_wave_points",
    "atr_unavailable",
    "invalid_structural_stop",
    "stop_too_far",
    "confidence_below_minimum",
    "duplicate_event",
]


def _primary_rejection_reason(direction_diagnostics: dict[str, dict[str, Any]]) -> str | None:
    best_reason = None
    best_rank = -1
    for detail in direction_diagnostics.values():
        outcome = detail.get("outcome")
        rank = _REJECTION_GATE_ORDER.index(outcome) if outcome in _REJECTION_GATE_ORDER else -1
        if rank > best_rank:
            best_rank = rank
            best_reason = outcome
    return best_reason


def evaluate_entry(
    db, candle: Candle, config: Config, diagnostics: dict[str, Any] | None = None
) -> EntryDecision | None:
    """Causal 1m entry check for a just-closed 1m candle.

    Every read is bounded by `<= decision_time` (the closing candle's close
    time). No candle that closes after this point can affect the outcome.

    If a `diagnostics` dict is passed in, it is filled in-place with the
    outcome of every gate this evaluation passed through, for logging
    purposes only - it never influences the decision returned.
    """
    if not config.enabled or candle.timeframe != ENTRY_TIMEFRAME:
        if diagnostics is not None:
            diagnostics["outcome"] = "strategy_disabled"
        return None
    decision_time = candle.close_time
    m1 = list(
        db.scalars(
            select(Candle)
            .where(
                Candle.symbol_id == candle.symbol_id,
                Candle.timeframe == ENTRY_TIMEFRAME,
                Candle.is_closed.is_(True),
                Candle.open_time <= candle.open_time,
            )
            .order_by(Candle.open_time.desc())
            .limit(200)
        )
    )
    m1.reverse()
    if len(m1) < max(20, config.ha_pullback_min_candles + 2) or m1[-1].id != candle.id:
        if diagnostics is not None:
            diagnostics["outcome"] = "insufficient_candle_history"
        return None
    ha1 = derive_heikin_ashi(m1)
    index = len(ha1) - 1
    direction_diagnostics: dict[str, dict[str, Any]] = {}
    for direction in ("bullish", "bearish"):
        detail: dict[str, Any] = {}
        reversal = confirmed_reversal(
            ha1, m1, index, direction,
            pullback_min=config.ha_pullback_min_candles,
            wick_body_max_ratio=config.ha_wick_body_max_ratio,
            body_atr_min_ratio=config.ha_body_atr_min_ratio,
            confirmation_required=config.ha_confirmation_required,
        )
        if not reversal:
            direction_diagnostics[direction] = {"outcome": "no_ha_reversal"}
            continue
        price = D(reversal["real_entry"])
        detail["real_entry"] = str(price)
        counts = list(
            db.scalars(
                select(ElliottWaveCount)
                .where(
                    ElliottWaveCount.symbol_id == candle.symbol_id,
                    ElliottWaveCount.timeframe == ELLIOTT_CONTEXT_TIMEFRAME,
                    ElliottWaveCount.direction == direction,
                    ElliottWaveCount.detected_at <= decision_time,
                )
                .order_by(ElliottWaveCount.detected_at.desc(), ElliottWaveCount.id.desc())
                .limit(10)
            )
        )
        if not counts:
            direction_diagnostics[direction] = {"outcome": "no_valid_wave3_context", **detail}
            continue
        count = next((c for c in counts if wave3_gate(c, decision_time, price)[0]), None)
        if not count:
            direction_diagnostics[direction] = {"outcome": "wave3_gate_failed", **detail}
            continue
        detail["elliott_wave_count_id"] = count.id
        detail["elliott_confidence"] = str(count.confidence_score)
        structure_event = db.scalar(
            select(MarketStructureEvent)
            .where(
                MarketStructureEvent.symbol_id == candle.symbol_id,
                MarketStructureEvent.timeframe == STRUCTURE_TIMEFRAME,
                MarketStructureEvent.direction == direction,
                MarketStructureEvent.detected_at <= decision_time,
            )
            .order_by(MarketStructureEvent.detected_at.desc())
            .limit(1)
        )
        if not structure_event:
            direction_diagnostics[direction] = {"outcome": "no_structure_event", **detail}
            continue
        points = sorted(count.points, key=lambda p: p.sequence_number)
        if len(points) < 3:
            direction_diagnostics[direction] = {"outcome": "insufficient_wave_points", **detail}
            continue
        wave0, wave1, wave2 = points[0], points[1], points[2]
        atr = atr_at(m1, index)
        if atr is None:
            direction_diagnostics[direction] = {"outcome": "atr_unavailable", **detail}
            continue
        detail["atr"] = str(atr)
        swing = _recent_confirmed_swing(db, candle.symbol_id, direction, decision_time)
        stop = structural_stop(direction, D(wave2.price), D(swing.price) if swing else None, atr, config.atr_stop_buffer)
        detail["stop"] = str(stop)
        wrong_side = stop >= price if direction == "bullish" else stop <= price
        too_far = abs(price - stop) > atr * config.max_stop_atr_ratio
        if too_far or wrong_side:
            direction_diagnostics[direction] = {
                "outcome": "invalid_structural_stop" if wrong_side else "stop_too_far",
                **detail,
            }
            continue
        score_breakdown = {
            "elliott_wave_confluence": float(count.confidence_score),
            "structure_confirmation_bonus": 10.0 if structure_event.event_type == "BOS" else 0.0,
            "structural_swing_alignment_bonus": 5.0 if swing else 0.0,
        }
        confidence_score = min(D("100"), D(str(sum(score_breakdown.values()))))
        detail["confidence_score"] = str(confidence_score)
        detail["minimum_confidence"] = str(config.minimum_confidence)
        if confidence_score < config.minimum_confidence:
            direction_diagnostics[direction] = {"outcome": "confidence_below_minimum", **detail}
            continue
        fingerprint = event_fingerprint(candle.symbol_id, direction, count.id, reversal["reversal_candle_id"])
        if not config.reentry_enabled and _already_used(db, candle.symbol_id, fingerprint):
            direction_diagnostics[direction] = {"outcome": "duplicate_event", **detail}
            continue
        entry_tolerance = atr * D("0.05")
        direction_diagnostics[direction] = {"outcome": "setup_created", **detail}
        if diagnostics is not None:
            diagnostics["outcome"] = "setup_created"
            diagnostics["direction"] = direction
            diagnostics["directions"] = direction_diagnostics
        return EntryDecision(
            direction=direction,
            entry=price,
            entry_min=price - entry_tolerance,
            entry_max=price + entry_tolerance,
            stop=stop,
            elliott_wave_count_id=count.id,
            structure_event_id=structure_event.id,
            confidence_score=confidence_score,
            score_breakdown=score_breakdown,
            conditions={
                "event_fingerprint": fingerprint,
                "wave0_price": str(wave0.price), "wave0_timestamp": wave0.timestamp.isoformat(),
                "wave1_price": str(wave1.price), "wave1_timestamp": wave1.timestamp.isoformat(),
                "wave2_price": str(wave2.price), "wave2_timestamp": wave2.timestamp.isoformat(),
                "expected_wave3_direction": direction,
                "invalidation_price": str(count.invalidation_price),
                "elliott_confirmation_timestamp": (count.confirmed_at or count.detected_at).isoformat(),
                "elliott_context_timeframe": ELLIOTT_CONTEXT_TIMEFRAME,
                "structure_timeframe": STRUCTURE_TIMEFRAME,
                "entry_timeframe": ENTRY_TIMEFRAME,
                "exit_timeframe": EXIT_TIMEFRAME,
                "reversal_candle_id": reversal["reversal_candle_id"],
                "confirmation_candle_id": reversal["confirmation_candle_id"],
                "ha_reversal_audit": {**reversal, "confirmed_at": reversal["confirmed_at"].isoformat()},
                "structural_swing_id": swing.id if swing else None,
            },
        )
    if diagnostics is not None:
        diagnostics["outcome"] = _primary_rejection_reason(direction_diagnostics)
        diagnostics["directions"] = direction_diagnostics
    return None


def wave3_still_intact(count: ElliottWaveCount, real_price: Decimal) -> bool:
    """Point-in-time invalidation check using the count's fixed invalidation price.

    Deliberately does not read `count.invalidated_at`: that column is
    mutated in place on the same row as later candles close, so reading it
    "as of now" rather than "as of decision_time" would leak future state
    into an earlier decision. `invalidation_price` is set once at count
    creation and never rewritten, so comparing it against the real price at
    the moment of the check is the only causal way to answer this.
    """
    if count is None or count.invalidation_price is None:
        return True
    return real_price > D(count.invalidation_price) if count.direction == "bullish" else real_price < D(count.invalidation_price)
