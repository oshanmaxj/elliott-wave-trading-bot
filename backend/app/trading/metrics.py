from decimal import Decimal
from math import sqrt


def calculate_metrics(pnls: list[Decimal], rs: list[Decimal], starting_balance: Decimal) -> dict:
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    equity, peak, max_dd = starting_balance, starting_balance, Decimal("0")
    curve = [{"index": 0, "equity": float(equity), "drawdown_pct": 0.0}]
    for index, value in enumerate(pnls, 1):
        equity += value
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak else Decimal("0")
        max_dd = max(max_dd, dd)
        curve.append({"index": index, "equity": float(equity), "drawdown_pct": float(dd)})
    total = len(pnls)
    mean_r = sum(rs, Decimal("0")) / len(rs) if rs else Decimal("0")
    variance = sum((x - mean_r) ** 2 for x in rs) / len(rs) if rs else Decimal("0")
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
    }
