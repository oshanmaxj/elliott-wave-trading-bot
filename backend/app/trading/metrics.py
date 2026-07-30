from decimal import Decimal
from math import sqrt


def calculate_metrics(pnls: list[Decimal], rs: list[Decimal], starting_balance: Decimal) -> dict:
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    equity, peak, max_dd = starting_balance, starting_balance, Decimal("0")
    curve = [{"index": 0, "equity": float(equity), "drawdown_pct": 0.0, "drawdown": 0.0}]
    max_dd_amount = Decimal("0")
    for index, value in enumerate(pnls, 1):
        equity += value
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak else Decimal("0")
        dd_amount = peak - equity
        max_dd = max(max_dd, dd)
        max_dd_amount = max(max_dd_amount, dd_amount)
        curve.append({"index": index, "equity": float(equity), "drawdown_pct": float(dd), "drawdown": float(dd_amount)})
    total = len(pnls)
    mean_r = sum(rs, Decimal("0")) / len(rs) if rs else Decimal("0")
    variance = sum((x - mean_r) ** 2 for x in rs) / len(rs) if rs else Decimal("0")
    def longest(positive):
        best = current = 0
        for value in pnls:
            current = current + 1 if (value > 0 if positive else value < 0) else 0
            best = max(best, current)
        return best
    extended = {
        "total_trades": total, "winning_trades": len(wins), "losing_trades": len(losses),
        "breakeven_trades": total - len(wins) - len(losses),
        "return_pct": str(sum(pnls, Decimal("0")) / starting_balance * 100 if starting_balance else 0),
        "average_winner": str(sum(wins, Decimal("0")) / len(wins) if wins else 0),
        "average_loser": str(sum(losses, Decimal("0")) / len(losses) if losses else 0),
        "largest_winner": str(max(wins) if wins else 0), "largest_loser": str(min(losses) if losses else 0),
        "max_drawdown_amount": str(max_dd_amount),
        "consecutive_wins": longest(True), "consecutive_losses": longest(False),
    }
    return {
        "trades_taken": total, "wins": len(wins), "losses": len(losses),
        "break_even": total - len(wins) - len(losses),
        "gross_profit": sum(wins, Decimal("0")), "gross_loss": abs(sum(losses, Decimal("0"))),
        "net_profit": sum(pnls, Decimal("0")),
        "profit_factor": sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0"))) if losses else None,
        "win_rate": Decimal(len(wins) * 100) / total if total else Decimal("0"),
        "max_drawdown_pct": max_dd, "expectancy": mean_r, "average_rr": mean_r,
        "sharpe_like_ratio": mean_r / Decimal(str(sqrt(float(variance)))) if variance > 0 else None,
        "equity_curve": curve,
        "drawdown_curve": [{"index": x["index"], "drawdown": x["drawdown"], "drawdown_pct": x["drawdown_pct"]} for x in curve],
        "extended": extended,
    }
