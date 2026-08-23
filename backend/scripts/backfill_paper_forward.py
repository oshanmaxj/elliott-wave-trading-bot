"""Backfill production-market paper forward results from persisted candles."""
import argparse
from datetime import datetime, timezone
import json

from app.database.session import SessionLocal
from app.trading.paper_forward import backfill


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol")
    parser.add_argument("--start", required=True, type=timestamp)
    parser.add_argument("--end", required=True, type=timestamp)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="persist results (default is dry-run)")
    mode.add_argument("--dry-run", action="store_true", help="report only")
    options = parser.parse_args()
    if options.end <= options.start:
        parser.error("--end must be after --start")
    with SessionLocal() as db:
        print(json.dumps(backfill(db, options.symbol, options.start, options.end, options.apply), indent=2, default=str))


if __name__ == "__main__":
    main()
