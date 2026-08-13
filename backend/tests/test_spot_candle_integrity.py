from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import router
from app.database.session import get_db
from app.market_data.binance_rest import BinanceRESTClient
from app.models import Candle
from app.repositories.market import ensure_symbol, upsert_candle
from app.services.historical_backfill import historical_backfill


def raw_klines(start_ms, step_ms, count=20):
    rows = []
    for index in range(count):
        opened = start_ms + index * step_ms
        base = Decimal("63000") + index
        rows.append(
            [
                opened,
                str(base),
                str(base + Decimal("25.5")),
                str(base - Decimal("20.25")),
                str(base + Decimal("5.75")),
                "12.345",
                opened + step_ms - 1,
                "777777.12",
                123,
                "6.1",
                "388888.56",
                "0",
            ]
        )
    return rows


@pytest.mark.parametrize(
    "symbol,timeframe,step_ms",
    [
        ("BTCUSDT", "1h", 3_600_000),
        ("BTCUSDT", "15m", 900_000),
        ("ETHUSDT", "1h", 3_600_000),
    ],
)
def test_raw_spot_kline_db_and_chart_api_are_identical(
    symbol, timeframe, step_ms, session_factory, monkeypatch
):
    raw = raw_klines(1_767_225_600_000, step_ms)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    normalized = [BinanceRESTClient.normalize_kline(row, now=now) for row in raw]
    with session_factory.begin() as db:
        symbol_row = ensure_symbol(db, symbol)
        for candle in normalized:
            upsert_candle(db, symbol_row.id, timeframe, candle)
        # Duplicate replay must update, not create a second timestamp identity.
        upsert_candle(db, symbol_row.id, timeframe, normalized[-1])
        stored = list(
            db.scalars(
                select(Candle)
                .where(
                    Candle.symbol_id == symbol_row.id,
                    Candle.timeframe == timeframe,
                )
                .order_by(Candle.open_time)
            )
        )
        assert symbol_row.market_type == "spot"
        assert len(stored) == 20
        for source, row in zip(raw, stored):
            stored_open = row.open_time.replace(tzinfo=timezone.utc) if row.open_time.tzinfo is None else row.open_time.astimezone(timezone.utc)
            stored_close = row.close_time.replace(tzinfo=timezone.utc) if row.close_time.tzinfo is None else row.close_time.astimezone(timezone.utc)
            assert int(stored_open.timestamp() * 1000) == source[0]
            assert int(stored_close.timestamp() * 1000) == source[6]
            assert (row.open, row.high, row.low, row.close) == tuple(
                Decimal(value) for value in source[1:5]
            )

    monkeypatch.setattr(historical_backfill, "session_factory", session_factory)
    monkeypatch.setattr(
        historical_backfill,
        "verify",
        lambda *args, **kwargs: {"coverage_complete": True},
    )
    app = FastAPI()
    app.include_router(router)

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    response = TestClient(app).get(
        "/api/candles",
        params={"symbol": symbol, "timeframe": timeframe, "limit": 20},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 20
    for source, row in zip(raw, payload):
        parsed_open = datetime.fromisoformat(row["open_time"].replace("Z", "+00:00"))
        parsed_close = datetime.fromisoformat(row["close_time"].replace("Z", "+00:00"))
        assert parsed_open.tzinfo is not None and parsed_open.utcoffset().total_seconds() == 0
        assert int(parsed_open.timestamp() * 1000) == source[0]
        assert int(parsed_close.timestamp() * 1000) == source[6]
        assert tuple(Decimal(str(row[key])) for key in ("open", "high", "low", "close")) == tuple(
            Decimal(value) for value in source[1:5]
        )
