"""Dry-run-first range regeneration for candle-derived analysis artifacts."""

import argparse
import asyncio
import json
from datetime import datetime, timezone

from app.services.derived_regeneration import DerivedRegenerationService


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


async def main(args):
    report = await DerivedRegenerationService().run(
        args.symbol, args.timeframe, timestamp(args.start), timestamp(args.end), args.apply
    )
    print(json.dumps(report, default=str, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True, choices=["1m", "5m", "15m", "1h", "4h"])
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args()))
