"""Audit DB candles against Binance and optionally apply authoritative OHLC replacements."""

import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.database.session import SessionLocal
from app.market_data.binance_rest import BinanceRESTClient
from app.models import Candle, Symbol
from app.repositories.market import upsert_candle


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


async def run(args):
    start, end = utc(args.start), utc(args.end)
    client = BinanceRESTClient(args.base_url)
    authoritative = await client.fetch_paginated(args.symbol, args.timeframe, start, end)
    source = {row.open_time: row for row in authoritative}
    changed = []
    with SessionLocal.begin() as db:
        symbol = db.scalar(select(Symbol).where(Symbol.symbol == args.symbol))
        if not symbol:
            raise SystemExit(f"Unknown symbol {args.symbol}")
        stored = list(db.scalars(select(Candle).where(
            Candle.symbol_id == symbol.id, Candle.timeframe == args.timeframe,
            Candle.open_time >= start, Candle.open_time <= end).order_by(Candle.open_time)))
        for candle in stored:
            key = candle.open_time
            if key.tzinfo is None:
                key = key.replace(tzinfo=timezone.utc)
            expected = source.get(key)
            if not expected:
                continue
            db_ohlc = tuple(getattr(candle, name) for name in ("open", "high", "low", "close"))
            api_ohlc = tuple(getattr(expected, name) for name in ("open", "high", "low", "close"))
            if db_ohlc != api_ohlc:
                changed.append({"candle_id": candle.id, "open_time": key.isoformat(),
                                "db": [str(x) for x in db_ohlc],
                                "binance": [str(x) for x in api_ohlc]})
                if args.apply:
                    upsert_candle(db, symbol.id, args.timeframe, expected)
        if not args.apply:
            db.rollback()
    print({"symbol": args.symbol, "timeframe": args.timeframe,
           "source_base_url": client.base_url, "authoritative_rows": len(source),
           "mismatches": changed, "applied": bool(args.apply)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True, choices=["1m", "5m", "15m", "1h", "4h"])
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--base-url", default="https://api.binance.com")
    parser.add_argument("--apply", action="store_true", help="Apply exact Binance values; default is audit only")
    asyncio.run(run(parser.parse_args()))
