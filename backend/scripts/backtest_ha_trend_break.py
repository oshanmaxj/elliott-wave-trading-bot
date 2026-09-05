"""Backfill the research-only Heikin Ashi trend-break audit table from production Spot candles."""
import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from app.database.session import SessionLocal
from app.models import Symbol
from app.strategies.heikin_ashi_trend_break_research import evaluate


def utc(value: str):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    parser.add_argument("--start", type=utc, required=True)
    parser.add_argument("--end", type=utc, required=True)
    parser.add_argument("--apply", action="store_true", help="persist audit rows")
    args = parser.parse_args()
    with SessionLocal() as db:
        symbol = db.scalar(select(Symbol).where(Symbol.symbol == args.symbol))
        if not symbol:
            raise SystemExit("symbol is not configured")
        rows = evaluate(db, symbol.id, args.start, args.end, persist=args.apply)
        closed = [row for row in rows if row.realized_r is not None]
        net = sum((row.realized_r for row in closed), 0)
        print({"symbol": args.symbol, "trades": len(closed), "net_r": str(net), "persisted": args.apply})


if __name__ == "__main__":
    main()
