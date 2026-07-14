from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.base import Base


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def make_candle(index, open_=100, high=105, low=95, close=102, volume=1000, closed=True):
    opened = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    return SimpleNamespace(id=index + 1, open_time=opened, close_time=opened + timedelta(hours=1) - timedelta(milliseconds=1), open=Decimal(str(open_)), high=Decimal(str(high)), low=Decimal(str(low)), close=Decimal(str(close)), volume=Decimal(str(volume)), is_closed=closed)

