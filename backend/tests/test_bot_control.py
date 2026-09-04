from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import auth as auth_api
from app.api import bot
from app.auth import hash_password
from app.database.session import get_db
from app.models import BotRuntimeState, ExchangeAccount, ExecutionEvent, User
from app.services.pipeline import automatic_routing_enabled


def make_client(session_factory, monkeypatch):
    config = SimpleNamespace(
        credential_encryption_key="test-encryption-key-that-is-stable-32",
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
    monkeypatch.setattr(
        bot,
        "user_stream",
        SimpleNamespace(
            restart=lambda: _done(True),
            stop=lambda: _done(None),
            status=lambda: {"running": False, "connected": False},
        ),
    )
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


async def _done(value):
    return value


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
                "enabled_timeframes_json": ["1m", "5m", "15m"],
                "enabled_strategies_json": ["wave_3_continuation"],
            },
        ).status_code
        == 200
    )
    assert api.get("/api/bot/config").json()["enabled_timeframes_json"] == [
        "1m",
        "5m",
        "15m",
    ]
    assert api.post("/api/bot/start", headers=csrf(api)).json()["status"] == "running"
    assert api.post("/api/auth/logout", headers=csrf(api)).status_code == 200
    assert api.get("/api/auth/me").status_code == 401


def test_control_state_transitions_resume_entries_without_clearing_kill_switch(
    session_factory, monkeypatch
):
    api = make_client(session_factory, monkeypatch)
    login(api)
    headers = csrf(api)
    configured = api.put(
        "/api/bot/config",
        headers=headers,
        json={
            "enabled_symbols_json": ["BTCUSDT"],
            "enabled_timeframes_json": ["1m"],
            "enabled_strategies_json": ["wave_3_continuation"],
            "manual_approval_required": False,
        },
    ).json()
    assert configured["manual_approval_required"] is False

    started = api.post("/api/bot/start", headers=headers).json()
    assert started["status"] == "running"
    assert started["automatic_trading_enabled"] is True
    assert started["pause_new_entries"] is False
    assert started["manual_approval_required"] is False

    paused = api.post("/api/bot/pause", headers=headers).json()
    assert paused["status"] == "running"
    assert paused["automatic_trading_enabled"] is True
    assert paused["pause_new_entries"] is True
    assert paused["kill_switch_enabled"] is False

    resumed = api.post("/api/bot/resume", headers=headers).json()
    assert resumed["status"] == "running"
    assert resumed["automatic_trading_enabled"] is True
    assert resumed["pause_new_entries"] is False

    stopped = api.post("/api/bot/stop", headers=headers).json()
    assert stopped["status"] == "stopped"
    restarted = api.post("/api/bot/start", headers=headers).json()
    assert restarted["status"] == "running"
    assert restarted["pause_new_entries"] is False

    emergency = api.post("/api/bot/emergency-stop", headers=headers).json()
    assert emergency["kill_switch_enabled"] is True
    assert emergency["pause_new_entries"] is True
    assert api.post("/api/bot/start", headers=headers).status_code == 409
    assert api.post("/api/bot/resume", headers=headers).status_code == 409

    with session_factory() as db:
        runtime = db.query(BotRuntimeState).one()
        events = list(
            db.query(ExecutionEvent)
            .filter(ExecutionEvent.event_type.like("bot_%"))
            .all()
        )
        assert runtime.kill_switch_enabled is True
        transition = next(row for row in events if row.event_type == "bot_pause")
        assert transition.metadata_json["requested_action"] == "pause"
        assert transition.metadata_json["actor"] == "admin@example.com"
        assert transition.metadata_json["previous_state"]["pause_new_entries"] is False
        assert transition.metadata_json["new_state"]["pause_new_entries"] is True
        assert transition.metadata_json["timestamp"]


def test_risk_config_rejects_tp_percentages_that_do_not_sum_to_100(
    session_factory, monkeypatch
):
    api = make_client(session_factory, monkeypatch)
    login(api)
    headers = csrf(api)
    response = api.put(
        "/api/bot/config",
        headers=headers,
        json={
            "risk_config_json": {
                "risk_per_trade_pct": "0.25",
                "tp1_pct": "50",
                "tp2_pct": "30",
                "tp3_pct": "30",
            }
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "tp_percentages_must_sum_to_100"


def test_risk_config_rejects_non_positive_max_open_positions(
    session_factory, monkeypatch
):
    api = make_client(session_factory, monkeypatch)
    login(api)
    headers = csrf(api)
    response = api.put(
        "/api/bot/config",
        headers=headers,
        json={"risk_config_json": {"max_open_positions": 0}},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "max_open_positions_must_be_a_positive_integer"


def test_risk_config_rejects_out_of_range_percentage(session_factory, monkeypatch):
    api = make_client(session_factory, monkeypatch)
    login(api)
    headers = csrf(api)
    response = api.put(
        "/api/bot/config",
        headers=headers,
        json={"risk_config_json": {"risk_per_trade_pct": "150"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "risk_per_trade_pct_must_be_between_0_and_100"


def test_risk_config_accepts_valid_payload(session_factory, monkeypatch):
    api = make_client(session_factory, monkeypatch)
    login(api)
    headers = csrf(api)
    response = api.put(
        "/api/bot/config",
        headers=headers,
        json={
            "risk_config_json": {
                "risk_per_trade_pct": "0.5",
                "max_open_positions": 2,
                "tp1_pct": "40",
                "tp2_pct": "30",
                "tp3_pct": "30",
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["risk_config_json"]["max_open_positions"] == 2


def test_resumed_runtime_is_automatically_routable(session_factory, monkeypatch):
    api = make_client(session_factory, monkeypatch)
    login(api)
    headers = csrf(api)
    api.put(
        "/api/bot/config",
        headers=headers,
        json={
            "enabled_symbols_json": ["BTCUSDT"],
            "enabled_timeframes_json": ["1m"],
            "enabled_strategies_json": ["wave_3_continuation"],
            "manual_approval_required": False,
        },
    )
    api.post("/api/bot/start", headers=headers)
    api.post("/api/bot/pause", headers=headers)
    api.post("/api/bot/resume", headers=headers)

    with session_factory() as db:
        assert automatic_routing_enabled(db.query(BotRuntimeState).one()) is True


def test_binance_credentials_encrypted_secret_never_returned(
    session_factory, monkeypatch
):
    api = make_client(session_factory, monkeypatch)
    raw = {
        "environment": "testnet",
        "api_key": "abcdefghAB12",
        "api_secret": "super-secret-value",
        "label": "test",
    }
    assert api.post("/api/binance/credentials", json=raw).status_code == 401
    login(api)
    assert api.post("/api/binance/credentials", json=raw).status_code == 403
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
    status = api.get("/api/binance/connection/status").json()
    assert status["credentials_saved"] is True
    assert status["connected"] is False
    assert status["masked_api_key"].endswith("AB12")
    assert raw["api_secret"] not in str(status)
    with session_factory() as db:
        _, restored = bot.stored_settings(db)
        assert restored.binance_api_key == raw["api_key"]
        assert restored.binance_api_secret == raw["api_secret"]
    logout_csrf = csrf(api)
    assert api.post("/api/auth/logout", headers=logout_csrf).status_code == 200
    assert (
        api.post("/api/binance/credentials", headers=logout_csrf, json=raw).status_code
        == 401
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
