from decimal import Decimal

from sqlalchemy import select

from app.models import PaperAccount, PaperPosition, TradeSetup
from app.trading.execution import candle_exit, execution_fee, pnl


def process_paper_candle(db, candle) -> list[PaperPosition]:
    """Advance paper records from one closed candle. This module has no exchange client."""
    positions = list(db.scalars(select(PaperPosition).where(
        PaperPosition.symbol_id == candle.symbol_id,
        PaperPosition.status.in_(["pending", "open", "partially_closed"]),
    )))
    changed = []
    for position in positions:
        setup = db.get(TradeSetup, position.trade_setup_id)
        account = db.get(PaperAccount, position.account_id)
        if not setup or not account:
            continue
        if position.status == "pending":
            if setup.expires_at < candle.open_time:
                position.status, position.exit_reason = "expired", "setup_expired"
                changed.append(position)
                continue
            if not (Decimal(candle.low) <= Decimal(setup.preferred_entry) <= Decimal(candle.high)):
                continue
            position.status, position.opened_at = "open", candle.close_time
            account.balance -= position.fees
        targets = [x for x in (position.tp1, position.tp2, position.tp3) if x is not None]
        if not targets:
            continue
        target = Decimal(targets[0])
        event = candle_exit(position.direction, Decimal(candle.high), Decimal(candle.low), Decimal(position.stop_loss), target, "stop_first")
        if event.price is None:
            continue
        if event.reason == "stop_loss":
            close_qty = Decimal(position.quantity)
            fee = execution_fee(Decimal(event.price), close_qty, Decimal("0.05"))
            realized = pnl(position.direction, Decimal(position.entry_price), Decimal(event.price), close_qty, fee)
            position.realized_pnl += realized
            position.fees += fee
            position.quantity = 0
            position.status, position.closed_at = "stopped", candle.close_time
            position.exit_price, position.exit_reason = event.price, "stop_loss"
        else:
            # TP2 closes 4/7 of the 70% remaining after TP1: 40% of original.
            fractions = [(D("0.30"), "tp1"), (D("0.5714285714285714"), "tp2"), (D("1"), "tp3")]
            hit_index = 0 if position.tp1 is not None else 1 if position.tp2 is not None else 2
            fraction, label = fractions[hit_index]
            close_qty = Decimal(position.quantity) if label == "tp3" else Decimal(position.quantity) * fraction
            fee = execution_fee(Decimal(event.price), close_qty, Decimal("0.05"))
            realized = pnl(position.direction, Decimal(position.entry_price), Decimal(event.price), close_qty, fee)
            position.realized_pnl += realized
            position.fees += fee
            position.quantity -= close_qty
            setattr(position, label, None)
            if label == "tp1":
                position.stop_loss = position.entry_price
            if position.quantity <= 0 or label == "tp3":
                position.status, position.closed_at = "closed", candle.close_time
                position.exit_price, position.exit_reason = event.price, label
                position.quantity = 0
            else:
                position.status = "partially_closed"
        account.realized_pnl += realized
        account.balance += realized
        account.equity = account.balance
        account.max_equity = max(account.max_equity, account.equity)
        account.drawdown_pct = (account.max_equity - account.equity) / account.max_equity * 100 if account.max_equity else 0
        changed.append(position)
    return changed


D = Decimal
