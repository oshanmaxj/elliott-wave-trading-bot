from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import bot
from app.database.session import get_db
from app.models import ExchangeAccount


def client(session_factory, monkeypatch):
    monkeypatch.setattr(
        bot,
        "settings",
        SimpleNamespace(
            execution_admin_token="admin-token",
            execution_trader_token="trader-token",
            execution_viewer_token="viewer-token",
            credential_encryption_key="test-encryption-key",
            binance_execution_enabled=False,
            binance_environment="testnet",
            execution_mode="disabled",
            allow_production_orders=False,
        ),
    )
    app = FastAPI()
    app.include_router(bot.router)

    def db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    return TestClient(app)


def test_bot_state_survives_requests_and_roles_are_enforced(
    session_factory, monkeypatch
):
    api = client(session_factory, monkeypatch)
    assert api.get("/api/bot/status").json()["status"] == "stopped"
    assert (
        api.put(
            "/api/bot/config",
            json={
                "enabled_symbols_json": ["BTCUSDT"],
                "enabled_strategies_json": ["wave_3_continuation"],
            },
        ).status_code
        == 401
    )
    headers = {"Authorization": "Bearer admin-token"}
    assert (
        api.put(
            "/api/bot/config",
            headers=headers,
            json={
                "enabled_symbols_json": ["BTCUSDT"],
                "enabled_strategies_json": ["wave_3_continuation"],
            },
        ).status_code
        == 200
    )
    assert api.post("/api/bot/start", headers=headers).json()["status"] == "running"
    assert api.get("/api/bot/status").json()["status"] == "running"
    assert (
        api.post("/api/bot/emergency-stop", headers=headers).json()[
            "kill_switch_enabled"
        ]
        is True
    )


def test_credentials_are_encrypted_and_never_returned(session_factory, monkeypatch):
    api = client(session_factory, monkeypatch)
    headers = {"Authorization": "Bearer admin-token"}
    raw = {
        "environment": "testnet",
        "api_key": "abcdefghAB12",
        "api_secret": "super-secret-value",
        "label": "test",
    }
    response = api.post("/api/binance/credentials", headers=headers, json=raw)
    assert (
        response.status_code == 200
        and "api_secret" not in response.text
        and "abcdefghAB12" not in response.text
    )
    with session_factory() as db:
        row = db.query(ExchangeAccount).one()
        assert (
            row.encrypted_api_key != raw["api_key"]
            and row.encrypted_api_secret != raw["api_secret"]
            and row.masked_api_key.endswith("AB12")
        )


def test_trader_can_pause_but_cannot_change_config(session_factory, monkeypatch):
    api = client(session_factory, monkeypatch)
    headers = {"Authorization": "Bearer trader-token"}
    assert api.post("/api/bot/pause", headers=headers).status_code == 200
    assert api.put("/api/bot/config", headers=headers, json={}).status_code == 403
