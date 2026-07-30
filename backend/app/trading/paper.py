"""Deterministic paper execution. This module has no exchange client or order API."""

from decimal import Decimal

from sqlalchemy import select

from app.models import BotLog, PaperAccount, PaperPosition, TradeSetup
from app.trading.execution import candle_exit, execution_fee, pnl, slipped_price

D = Decimal


def paper_log(db, event_type: str, position: PaperPosition, **context) -> None:
    db.add(BotLog(
        level="INFO", service="paper", event_type=event_type,
        message=f"Paper position {position.id} {event_type.replace('_', ' ')}",
        context_json={
            "paper_position_id": position.id, "trade_setup_id": position.trade_setup_id,
            "account_id": position.account_id, **context,
        },
    ))


def _account_update(account: PaperAccount, realized: Decimal) -> None:
    account.realized_pnl += realized
    account.balance += realized
    account.equity = account.balance
    account.max_equity = max(account.max_equity, account.equity)
    account.drawdown_pct = (
        (account.max_equity - account.equity) / account.max_equity * 100
        if account.max_equity else 0
    )


def _close_quantity(db, position, account, raw_price, quantity, reason, closed_at):
    exit_price = slipped_price(
        D(raw_price), position.direction, D(position.slippage_bps), False
    )
    fee = execution_fee(exit_price, quantity, D(position.taker_fee_pct))
    realized = pnl(position.direction, D(position.entry_price), exit_price, quantity, fee)
    position.realized_pnl += realized
    position.realized_r = (
        position.realized_pnl / position.risk_amount if position.risk_amount else 0
    )
    position.fees += fee
    position.slippage += abs(exit_price - D(raw_price)) * quantity
    position.quantity -= quantity
    position.exit_price = exit_price
    _account_update(account, realized)
    if position.quantity <= 0:
        position.quantity = 0
        position.status, position.closed_at, position.exit_reason = "closed", closed_at, reason
        paper_log(db, "paper_position_closed", position, reason=reason)
    return realized


def process_paper_candle(db, candle) -> list[PaperPosition]:
    positions = list(db.scalars(select(PaperPosition).where(
        PaperPosition.symbol_id == candle.symbol_id,
        PaperPosition.status.in_(["waiting_entry", "pending", "open", "partially_closed"]),
    )))
    changed = []
    for position in positions:
        setup = db.get(TradeSetup, position.trade_setup_id)
        account = db.get(PaperAccount, position.account_id)
        if not setup or not account:
            continue
        if position.status in {"waiting_entry", "pending"}:
            if setup.expires_at < candle.open_time:
                position.status, position.exit_reason = "expired", "setup_expired"
                setup.status = "expired"
                paper_log(db, "setup_expired", position)
                changed.append(position)
                continue
            if setup.invalidation_price is not None and (
                (position.direction == "bullish" and candle.low <= setup.invalidation_price)
                or (position.direction == "bearish" and candle.high >= setup.invalidation_price)
            ):
                position.status, position.exit_reason = "invalidated", "setup_invalidated"
                setup.status, setup.invalidated_at = "invalidated", candle.close_time
                paper_log(db, "setup_invalidated", position)
                changed.append(position)
                continue
            if not (D(candle.high) >= D(setup.entry_min) and D(candle.low) <= D(setup.entry_max)):
                continue
            position.status, position.opened_at = "open", candle.close_time
            setup.status, setup.triggered_at = "triggered", candle.close_time
            position.realized_pnl -= position.fees
            _account_update(account, -D(position.fees))
            paper_log(db, "entry_triggered", position, candle_id=candle.id)
            paper_log(db, "paper_position_opened", position, entry_price=str(position.entry_price))

        targets = [x for x in (position.tp1, position.tp2, position.tp3) if x is not None]
        if not targets:
            continue
        event = candle_exit(
            position.direction, D(candle.high), D(candle.low), D(position.stop_loss),
            D(targets[0]), "stop_first",
        )
        if event.price is None:
            continue
        if event.reason == "stop_loss":
            _close_quantity(
                db, position, account, event.price, D(position.quantity),
                "stop_loss", candle.close_time,
            )
            paper_log(db, "paper_sl_hit", position, candle_id=candle.id)
        else:
            label = "tp1" if position.tp1 is not None else "tp2" if position.tp2 is not None else "tp3"
            fraction = {"tp1": D("0.30"), "tp2": D("0.5714285714285714"), "tp3": D("1")}[label]
            close_qty = D(position.quantity) if label == "tp3" else D(position.quantity) * fraction
            _close_quantity(db, position, account, event.price, close_qty, label, candle.close_time)
            setattr(position, label, None)
            paper_log(db, "paper_tp_hit", position, target=label, candle_id=candle.id)
            if label == "tp1":
                position.stop_loss = position.entry_price
            if position.quantity > 0:
                position.status = "partially_closed"
        changed.append(position)
    return changed


def manual_close(db, position: PaperPosition, raw_price: Decimal, closed_at, slippage_bps=None):
    account = db.get(PaperAccount, position.account_id)
    if slippage_bps is not None:
        position.slippage_bps = slippage_bps
    _close_quantity(
        db, position, account, raw_price, D(position.quantity), "manual_close", closed_at
    )
    return position
