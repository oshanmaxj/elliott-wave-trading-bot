from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERSIONS = BACKEND_ROOT / "alembic" / "versions"


def alembic_config(monkeypatch, database_path: Path) -> Config:
    url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def column_names(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def insert_existing_market_data(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO symbols
                (id, exchange, symbol, base_asset, quote_asset, market_type,
                 is_active, created_at, updated_at)
                VALUES
                (1, 'binance', 'BTCUSDT', 'BTC', 'USDT', 'usdt_perpetual',
                 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO candles
                (id, symbol_id, timeframe, open_time, close_time, open, high, low,
                 close, volume, quote_volume, trade_count, taker_buy_base_volume,
                 taker_buy_quote_volume, is_closed, created_at, updated_at)
                VALUES
                (1, 1, '1h', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 100, 110, 90,
                 105, 1000, 105000, 10, 500, 52500, 1,
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO analysis_snapshots
                (id, symbol_id, timeframe, trend, latest_structure_event,
                 active_fvg_count, indicator_values_json, confidence_score,
                 generated_at, created_at)
                VALUES
                (1, 1, '1h', 'bullish', NULL, 0, '{}', 75,
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
            )
        )


def assert_existing_data(engine) -> None:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM symbols")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM candles")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM analysis_snapshots")) == 1


def test_fresh_revision_by_revision_upgrade(monkeypatch, tmp_path):
    config = alembic_config(monkeypatch, tmp_path / "fresh.db")
    engine = create_engine(config.get_main_option("sqlalchemy.url"))

    command.upgrade(config, "0001")
    assert "liquidity_sweeps" not in inspect(engine).get_table_names()
    command.upgrade(config, "0002")
    assert "trade_setups" not in inspect(engine).get_table_names()

    command.upgrade(config, "0003")
    tables = set(inspect(engine).get_table_names())
    assert {"liquidity_sweeps", "trade_setups"} <= tables
    assert "elliott_wave_count_id" not in column_names(engine, "trade_setups")
    assert "elliott_wave_counts" not in tables

    command.upgrade(config, "0004")
    tables = set(inspect(engine).get_table_names())
    assert {"elliott_wave_counts", "elliott_wave_points"} <= tables
    assert "elliott_wave_count_id" in column_names(engine, "trade_setups")
    foreign_keys = inspect(engine).get_foreign_keys("trade_setups")
    wave_fk = next(
        fk
        for fk in foreign_keys
        if fk["constrained_columns"] == ["elliott_wave_count_id"]
    )
    assert wave_fk["name"] == "fk_trade_setups_elliott_wave_count_id"
    assert wave_fk["referred_table"] == "elliott_wave_counts"
    assert wave_fk["referred_columns"] == ["id"]

    output = StringIO()
    config.stdout = output
    command.current(config)
    assert "0004" in output.getvalue()
    engine.dispose()


def test_upgrade_from_0002_preserves_data_and_downgrades_safely(monkeypatch, tmp_path):
    config = alembic_config(monkeypatch, tmp_path / "existing.db")
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    command.upgrade(config, "0002")
    insert_existing_market_data(engine)

    command.upgrade(config, "head")
    assert_existing_data(engine)

    command.downgrade(config, "0003")
    tables = set(inspect(engine).get_table_names())
    assert "elliott_wave_points" not in tables
    assert "elliott_wave_counts" not in tables
    assert "elliott_wave_count_id" not in column_names(engine, "trade_setups")
    assert_existing_data(engine)

    command.downgrade(config, "0002")
    tables = set(inspect(engine).get_table_names())
    assert "trade_setups" not in tables
    assert "liquidity_sweeps" not in tables
    assert "status" not in column_names(engine, "liquidity_pools")
    assert "ix_liquidity_pools_status" not in {
        index["name"] for index in inspect(engine).get_indexes("liquidity_pools")
    }
    assert_existing_data(engine)
    engine.dispose()


def test_historical_migrations_do_not_import_application_models():
    for revision in (
        "0001_initial_schema.py",
        "0002_smart_money_concepts.py",
        "0003_liquidity_sweeps_trade_setups.py",
        "0004_elliott_wave_engine.py",
    ):
        source = (VERSIONS / revision).read_text(encoding="utf-8")
        assert "from app" not in source
        assert "import app" not in source
