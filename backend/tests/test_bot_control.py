from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import auth as auth_api
from app.api import bot
from app.auth import hash_password
from app.database.session import get_db
from app.models import ExchangeAccount, User


def make_client(session_factory, monkeypatch):
    config = SimpleNamespace(
        credential_encryption_key="test-encryption-key",
        binance_execution_enabled=False,
        binance_environment="testnet",
        execution_mode="disabled",
        allow_production_orders=False,
        auth_session_hours=24,
        auth_cookie_secure=False,
    )
    config.model_copy = lambda update: SimpleNamespace(**{**vars(config), **update})
    monkeypatch.setattr(bot, "settings", config)
    monkeypatch.setattr(auth_api, "settings", config)
    app = FastAPI()
    app.include_router(auth_api.router)
    app.include_router(bot.router)

    def db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    with session_factory.begin() as session:
        session.add(
            User(
                username="admin@example.com",
                password_hash=hash_password("correct horse battery staple"),
                role="admin",
                is_active=True,
            )
        )
    return TestClient(app)


def login(api, password="correct horse battery staple"):
    response = api.post(
        "/api/auth/login", json={"username": "admin@example.com", "password": password}
    )
    return response


def csrf(api):
    return {"X-CSRF-Token": api.cookies.get("wavescope_csrf")}


def test_login_success_failure_and_session_persistence(session_factory, monkeypatch):
    api = make_client(session_factory, monkeypatch)
    assert login(api, "wrong-password").status_code == 401
    response = login(api)
    assert response.status_code == 200 and response.json()["user"]["role"] == "admin"
    assert api.cookies.get("wavescope_session")
    assert api.get("/api/auth/me").json()["username"] == "admin@example.com"
    second = TestClient(api.app)
    second.cookies.update(api.cookies)
    assert second.get("/api/auth/me").status_code == 200


def test_protection_csrf_logout_and_authorization(session_factory, monkeypatch):
    api = make_client(session_factory, monkeypatch)
    assert api.get("/api/bot/status").status_code == 401
    login(api)
    assert api.put("/api/bot/config", json={}).status_code == 403
    assert (
        api.put(
            "/api/bot/config",
            headers=csrf(api),
            json={
                "enabled_symbols_json": ["BTCUSDT"],
                "enabled_strategies_json": ["wave_3_continuation"],
            },
        ).status_code
        == 200
    )
    assert api.post("/api/bot/start", headers=csrf(api)).json()["status"] == "running"
    assert api.post("/api/auth/logout", headers=csrf(api)).status_code == 200
    assert api.get("/api/auth/me").status_code == 401


def test_binance_credentials_encrypted_secret_never_returned(
    session_factory, monkeypatch
):
    api = make_client(session_factory, monkeypatch)
    login(api)
    raw = {
        "environment": "testnet",
        "api_key": "abcdefghAB12",
        "api_secret": "super-secret-value",
        "label": "test",
    }
    response = api.post("/api/binance/credentials", headers=csrf(api), json=raw)
    assert (
        response.status_code == 200
        and raw["api_secret"] not in response.text
        and raw["api_key"] not in response.text
    )
    with session_factory() as db:
        row = db.query(ExchangeAccount).one()
        assert (
            row.encrypted_api_key != raw["api_key"]
            and row.encrypted_api_secret != raw["api_secret"]
            and row.masked_api_key.endswith("AB12")
        )


def test_authenticated_binance_connection_test(session_factory, monkeypatch):
    api = make_client(session_factory, monkeypatch)
    login(api)
    api.post(
        "/api/binance/credentials",
        headers=csrf(api),
        json={
            "environment": "testnet",
            "api_key": "abcdefghAB12",
            "api_secret": "super-secret-value",
        },
    )

    class Client:
        def __init__(self, settings):
            pass

        async def sync_time(self):
            return 12

        async def account(self):
            return {
                "accountType": "SPOT",
                "canTrade": True,
                "canWithdraw": False,
                "canDeposit": True,
                "balances": [],
            }

        async def close(self):
            pass

    monkeypatch.setattr(bot, "BinanceSpotClient", Client)
    response = api.post("/api/binance/connection/test", headers=csrf(api))
    assert (
        response.status_code == 200
        and response.json()["connected"] is True
        and response.json()["withdrawal"] is False
    )
