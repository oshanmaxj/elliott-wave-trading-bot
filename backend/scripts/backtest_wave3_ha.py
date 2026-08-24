"""Backfill the research-only Wave-3 HA audit table from production Spot candles."""
import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from app.database.session import SessionLocal
from app.models import Symbol
from app.strategies.research import evaluate


def utc(value: str):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    parser.add_argument("--start", type=utc, required=True)
    parser.add_argument("--end", type=utc, required=True)
    parser.add_argument("--variant", choices=("A", "B"), action="append")
    parser.add_argument("--apply", action="store_true", help="persist audit rows")
    args = parser.parse_args()
    with SessionLocal() as db:
        symbol = db.scalar(select(Symbol).where(Symbol.symbol == args.symbol))
        if not symbol:
            raise SystemExit("symbol is not configured")
        for variant in args.variant or ("A", "B"):
            rows = evaluate(db, symbol.id, args.start, args.end, variant=variant, persist=args.apply)
            closed = [row for row in rows if row.realized_r is not None]
            net = sum((row.realized_r for row in closed), 0)
            print({"symbol": args.symbol, "variant": variant, "trades": len(closed), "net_r": str(net), "persisted": args.apply})


if __name__ == "__main__":
    main()
