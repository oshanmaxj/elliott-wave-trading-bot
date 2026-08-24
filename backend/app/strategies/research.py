"""Production-Spot research replay for the Wave-3 HA strategy."""

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import select

from app.models import (Candle, ElliottWaveCount, FVGZone, LiquiditySweep,
                        MarketStructureEvent, OrderBlock, SwingPoint,
                        Wave3HAResearchSignal)
from app.strategies.heikin_ashi import atr_at, confirmed_reversal, derive_heikin_ashi
from app.strategies.elliott_wave3_heikin_ashi_reversal import (
    ResearchParameters, score_components, structural_stop, total_score,
    wave3_gate,
)

D = Decimal


def _asof(rows, decision_time):
    return [row for row in rows if row.detected_at <= decision_time]


def _aligned(row, direction):
    return row is not None and row.direction == direction


def _simulate(direction, variant, entry, stop, after, opposite_exit):
    risk = abs(entry - stop)
    if not risk:
        return None
    remaining, realized = D("1"), D("0")
    hit_1r = hit_2r = False
    mfe = mae = D("0")
    exit_price, exit_reason, exit_time, exit_ha = after[-1].close, "end_of_research", after[-1].close_time, None
    for candle in after:
        favorable = D(candle.high) - entry if direction == "bullish" else entry - D(candle.low)
        adverse = entry - D(candle.low) if direction == "bullish" else D(candle.high) - entry
        mfe, mae = max(mfe, favorable), max(mae, adverse)
        stopped = D(candle.low) <= stop if direction == "bullish" else D(candle.high) >= stop
        if stopped:  # Hard protection always wins same-candle ambiguity.
            realized -= remaining
            exit_price, exit_reason, exit_time = stop, "hard_stop", candle.close_time
            remaining = D("0")
            break
        if variant == "B":
            one, two = entry + risk, entry + risk * 2
            if direction == "bearish":
                one, two = entry - risk, entry - risk * 2
            if not hit_1r and ((D(candle.high) >= one) if direction == "bullish" else (D(candle.low) <= one)):
                realized += D("0.25")
                remaining -= D("0.25")
                hit_1r = True
            if not hit_2r and ((D(candle.high) >= two) if direction == "bullish" else (D(candle.low) <= two)):
                realized += D("0.50")
                remaining -= D("0.25")
                hit_2r = True
        exit_signal = next((x for x in opposite_exit if x[0] <= candle.close_time), None)
        if exit_signal:
            exit_time, exit_price, exit_ha = exit_signal
            runner_r = ((D(exit_price) - entry) if direction == "bullish" else (entry - D(exit_price))) / risk
            realized += remaining * runner_r
            remaining = D("0")
            exit_reason = "5m_opposite_ha_reversal"
            break
    if remaining:
        runner_r = ((D(exit_price) - entry) if direction == "bullish" else (entry - D(exit_price))) / risk
        realized += remaining * runner_r
    return {"real_exit": D(exit_price), "exit_reason": exit_reason, "exit_time": exit_time,
            "exit_ha_candle_id": exit_ha, "realized_r": realized,
            "mfe_r": mfe / risk, "mae_r": mae / risk}


def evaluate(db, symbol_id: int, start, end, *, variant="A", params=None, persist=False):
    """Chronological MTF replay. Every artifact is bounded by its detected_at time."""
    params = params or ResearchParameters()
    warmup = start - timedelta(days=7)
    by_tf = {tf: list(db.scalars(select(Candle).where(
        Candle.symbol_id == symbol_id, Candle.timeframe == tf, Candle.is_closed.is_(True),
        Candle.close_time >= warmup, Candle.close_time <= end,
    ).order_by(Candle.close_time, Candle.id))) for tf in ("1m", "5m", "15m")}
    if not all(by_tf.values()):
        return []
    ha1, ha5 = derive_heikin_ashi(by_tf["1m"]), derive_heikin_ashi(by_tf["5m"])
    counts = list(db.scalars(select(ElliottWaveCount).where(
        ElliottWaveCount.symbol_id == symbol_id, ElliottWaveCount.timeframe == "15m",
        ElliottWaveCount.detected_at <= end,
    ).order_by(ElliottWaveCount.detected_at, ElliottWaveCount.id)))
    structures = list(db.scalars(select(MarketStructureEvent).where(MarketStructureEvent.symbol_id == symbol_id, MarketStructureEvent.timeframe == "5m", MarketStructureEvent.detected_at <= end).order_by(MarketStructureEvent.detected_at)))
    fvgs = list(db.scalars(select(FVGZone).where(FVGZone.symbol_id == symbol_id, FVGZone.timeframe == "5m", FVGZone.detected_at <= end).order_by(FVGZone.detected_at)))
    blocks = list(db.scalars(select(OrderBlock).where(OrderBlock.symbol_id == symbol_id, OrderBlock.timeframe == "5m", OrderBlock.detected_at <= end).order_by(OrderBlock.detected_at)))
    sweeps = list(db.scalars(select(LiquiditySweep).where(LiquiditySweep.symbol_id == symbol_id, LiquiditySweep.timeframe == "5m", LiquiditySweep.detected_at <= end).order_by(LiquiditySweep.detected_at)))
    swings = list(db.scalars(select(SwingPoint).where(SwingPoint.symbol_id == symbol_id, SwingPoint.timeframe == "5m", SwingPoint.detected_at <= end).order_by(SwingPoint.detected_at)))
    output = []
    for index, confirmation in enumerate(ha1):
        decision = confirmation.close_time
        if decision < start or decision > end:
            continue
        for direction in ("bullish", "bearish"):
            reversal = confirmed_reversal(ha1, by_tf["1m"], index, direction,
                pullback_min=params.ha_pullback_min_candles,
                wick_body_max_ratio=params.ha_wick_body_max_ratio,
                body_atr_min_ratio=params.ha_body_atr_min_ratio,
                confirmation_required=params.ha_confirmation_required)
            if not reversal:
                continue
            price = D(reversal["real_entry"])
            count = next((c for c in reversed(counts) if c.detected_at <= decision and c.direction == direction and wave3_gate(c, decision, price)[0]), None)
            if not count:
                continue
            visible = [x for x in structures if x.detected_at <= decision and x.direction == direction]
            bos = next((x for x in reversed(visible) if x.event_type == "BOS"), None)
            if not bos:
                continue
            choch = next((x for x in reversed(visible) if x.event_type == "CHoCH" and x.detected_at <= bos.detected_at), None)
            fvg = next((x for x in reversed(_asof(fvgs, decision)) if _aligned(x, direction)), None)
            block = next((x for x in reversed(_asof(blocks, decision)) if _aligned(x, direction)), None)
            sweep = next((x for x in reversed(_asof(sweeps, decision)) if _aligned(x, direction)), None)
            recent5 = [x for x in by_tf["5m"] if x.close_time <= decision]
            volume = len(recent5) >= 21 and D(recent5[-1].volume) > sum((D(x.volume) for x in recent5[-21:-1]), D("0")) / D("20")
            components = score_components(bos=True, choch_bos=bool(choch), ha_reversal=True,
                fvg=bool(fvg), order_block=bool(block), sweep=bool(sweep), volume=volume, alignment=True)
            score = total_score(components)
            if score < params.research_score_threshold:
                continue
            points = sorted(count.points, key=lambda p: p.sequence_number)
            wave2 = D(points[2].price)
            eligible_swings = [x for x in swings if x.detected_at <= decision and x.swing_type == ("low" if direction == "bullish" else "high")]
            swing = eligible_swings[-1] if eligible_swings else None
            atr = atr_at(by_tf["1m"], index)
            if atr is None:
                continue
            stop = structural_stop(direction, wave2, D(swing.price) if swing else None, atr, params.stop_loss_atr_buffer)
            if abs(price - stop) > atr * params.max_stop_atr_ratio or (stop >= price if direction == "bullish" else stop <= price):
                continue
            opposite = []
            opposite_direction = "bearish" if direction == "bullish" else "bullish"
            for j, row in enumerate(ha5):
                if row.close_time <= decision:
                    continue
                sig = confirmed_reversal(ha5, by_tf["5m"], j, opposite_direction,
                    pullback_min=params.ha_pullback_min_candles,
                    wick_body_max_ratio=params.ha_wick_body_max_ratio,
                    body_atr_min_ratio=params.ha_body_atr_min_ratio,
                    confirmation_required=params.ha_confirmation_required)
                if sig:
                    opposite.append((row.close_time, row.real_close, row.candle_id))
            after = [x for x in by_tf["1m"] if x.close_time > decision]
            if not after:
                continue
            result = _simulate(direction, variant, price, stop, after, opposite)
            fingerprint = sha256(f"{symbol_id}:{direction}:{count.id}:{reversal['reversal_candle_id']}".encode()).hexdigest()
            artifact_ids = {"bos": bos.id, "choch": choch.id if choch else None, "fvg": fvg.id if fvg else None,
                            "order_block": block.id if block else None, "liquidity_sweep": sweep.id if sweep else None,
                            "structural_swing": swing.id if swing else None}
            row = Wave3HAResearchSignal(symbol_id=symbol_id, variant=variant, direction=direction, status="closed",
                event_fingerprint=fingerprint, decision_time=decision, elliott_count_id=count.id,
                closed_at=result["exit_time"], holding_seconds=max(0, int((result["exit_time"] - decision).total_seconds())),
                wave_point_ids_json=[p.id for p in points], artifact_ids_json=artifact_ids,
                score=score, score_components_json=components,
                audit_json={"15m_elliott_state": count.metadata_json, "5m_structure": [x.id for x in visible[-10:]], **reversal},
                reversal_candle_id=reversal["reversal_candle_id"], confirmation_candle_id=reversal["confirmation_candle_id"],
                real_entry=price, real_stop=stop, real_exit=result["real_exit"], exit_reason=result["exit_reason"],
                exit_ha_candle_id=result["exit_ha_candle_id"], realized_r=result["realized_r"],
                mfe_r=result["mfe_r"], mae_r=result["mae_r"], volatility_regime="high" if atr / price > D("0.005") else "normal")
            output.append(row)
            if persist:
                exists = db.scalar(select(Wave3HAResearchSignal.id).where(Wave3HAResearchSignal.event_fingerprint == fingerprint, Wave3HAResearchSignal.variant == variant))
                if not exists:
                    db.add(row)
    if persist:
        db.commit()
    return output
