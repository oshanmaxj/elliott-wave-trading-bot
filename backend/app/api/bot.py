from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.constants import SUPPORTED_TIMEFRAMES
from app.auth import current_user, require_roles
from app.database.session import get_db
from app.execution.binance import BinanceSpotClient
from app.execution.credentials import credential_cipher, load_stored_settings
from app.services.binance_user_stream import user_stream
from app.market_data.binance_ws import market_stream
from app.models import (
    BotRuntimeState,
    BotLog,
    ExchangeAccount,
    ExecutionEvent,
    ExecutionOrder,
    LivePosition,
)

router = APIRouter(
    prefix="/api", tags=["bot-control"], dependencies=[Depends(current_user)]
)
settings = get_settings()
STRATEGIES = [
    "wave_3_continuation",
    "wave_5_continuation",
    "c_wave_reversal",
    "abc_zigzag_completion",
    "impulse_continuation",
    "bos_continuation",
    "choch_reversal",
    "liquidity_sweep_reversal",
    "order_block_retest",
    "fvg_retest",
    "premium_discount_entry",
    "elliott_bos",
    "elliott_choch",
    "elliott_liquidity_sweep",
    "elliott_fvg",
    "multi_timeframe_continuation",
    "smc_full_confluence",
]


require = require_roles


def state(db):
    row = db.scalar(select(BotRuntimeState).limit(1))
    if not row:
        row = BotRuntimeState(
            environment="testnet",
            enabled_symbols_json=["BTCUSDT", "ETHUSDT"],
            enabled_timeframes_json=["15m", "1h", "4h"],
            enabled_strategies_json=[],
            risk_config_json={
                "risk_per_trade_pct": "0.25",
                "daily_loss_pct": "1.0",
                "weekly_loss_pct": "3",
                "max_drawdown_pct": "10",
                "max_open_positions": 1,
                "max_symbol_exposure_pct": "10",
                "max_total_exposure_pct": "20",
                "minimum_confidence": "75",
                "minimum_rr": "2",
                "tp1_pct": "40",
                "tp2_pct": "30",
                "tp3_pct": "30",
            },
        )
        db.add(row)
        db.commit()
    return row


def clean(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def event(db, event_type, message, severity="INFO"):
    db.add(
        ExecutionEvent(
            severity=severity,
            event_type=event_type,
            exchange="binance",
            environment=state(db).environment,
            message=message,
            metadata_json={},
        )
    )
    db.commit()


@router.get("/bot/status")
def bot_status(db: Session = Depends(get_db)):
    row = state(db)
    account = db.scalar(select(ExchangeAccount).order_by(ExchangeAccount.id.desc()))
    active = db.scalar(
        select(func.count())
        .select_from(LivePosition)
        .where(LivePosition.status.in_(["open", "partially_closed"]))
    )
    return {
        **clean(row),
        "active_trades": active,
        "connected": bool(account and account.status == "connected"),
        "masked_api_key": account.masked_api_key if account else "",
        "market_stream": market_stream.status(),
        "user_stream": user_stream.status(),
        "production_locked": not (
            settings.binance_execution_enabled
            and settings.binance_environment == "production"
            and settings.execution_mode == "live"
            and settings.allow_production_orders
        ),
    }


@router.get("/bot/config")
def get_config(db: Session = Depends(get_db)):
    return clean(state(db))


@router.put("/bot/config")
def put_config(
    body: dict, db: Session = Depends(get_db), current=Depends(require("admin"))
):
    row = state(db)
    for key in (
        "automatic_trading_enabled",
        "manual_approval_required",
        "pause_new_entries",
        "enabled_symbols_json",
        "enabled_timeframes_json",
        "enabled_strategies_json",
        "strategy_config_json",
        "risk_config_json",
    ):
        if key in body:
            setattr(row, key, body[key])
    if not row.enabled_timeframes_json or not set(row.enabled_timeframes_json) <= SUPPORTED_TIMEFRAMES:
        raise HTTPException(422, "Unsupported timeframe")
    if not set(row.enabled_strategies_json) <= set(STRATEGIES):
        raise HTTPException(422, "Unsupported strategy")
    db.commit()
    event(db, "bot_config_updated", f"Bot configuration updated by {current}")
    return clean(row)


def transition(db, status, user):
    row = state(db)
    now = datetime.now(timezone.utc)
    row.status = status
    row.pause_new_entries = status != "running"
    if status == "running":
        row.started_at = now
        row.started_by = user
    else:
        row.stopped_at = now
        row.stopped_by = user
    db.commit()
    event(db, f"bot_{status}", f"Bot is now {status}")
    return clean(row)


@router.post("/bot/start")
def start(db: Session = Depends(get_db), current=Depends(require("admin"))):
    row = state(db)
    if row.environment == "production":
        raise HTTPException(423, "Production bot start remains locked")
    if row.kill_switch_enabled:
        raise HTTPException(409, "Disable the kill switch before starting")
    if not row.enabled_symbols_json or not row.enabled_strategies_json:
        raise HTTPException(409, "Enable at least one symbol and strategy")
    return transition(db, "running", current)


@router.post("/bot/stop")
def stop(db: Session = Depends(get_db), current=Depends(require("admin"))):
    return transition(db, "stopped", current)


@router.post("/bot/pause")
def pause(db: Session = Depends(get_db), current=Depends(require("admin", "trader"))):
    return transition(db, "paused", current)


@router.post("/bot/resume")
def resume(db: Session = Depends(get_db), current=Depends(require("admin", "trader"))):
    return transition(db, "running", current)


@router.post("/bot/emergency-stop")
def emergency(db: Session = Depends(get_db), current=Depends(require("admin"))):
    row = state(db)
    row.kill_switch_enabled = True
    row.status = "kill_switch_active"
    row.pause_new_entries = True
    db.commit()
    event(
        db,
        "execution_kill_switch_enabled",
        f"Emergency stop activated by {current}",
        "CRITICAL",
    )
    return clean(row)


@router.get("/bot/activity")
def activity(db: Session = Depends(get_db)):
    return [
        clean(x)
        for x in db.scalars(
            select(ExecutionEvent).order_by(ExecutionEvent.created_at.desc()).limit(50)
        )
    ]


@router.get("/strategy-diagnostics")
def strategy_diagnostics(
    period: str = Query("24h", pattern="^(1h|24h|48h)$"),
    db: Session = Depends(get_db),
):
    """Authenticated, database-backed health summary of the live strategy path."""
    since = datetime.now(timezone.utc) - timedelta(hours=int(period[:-1]))
    logs = list(db.scalars(select(BotLog).where(BotLog.created_at >= since, BotLog.service == "strategy_pipeline")))
    counts = {}
    for event_type in ("closed_candle_processed", "strategy_evaluation", "candidate_generated", "candidate_rejected", "setup_persisted", "execution_eligible"):
        counts[event_type] = sum(row.event_type == event_type for row in logs)
    aliases = {
        "confidence below threshold": "confidence_below_threshold",
        "reward-to-risk below threshold": "rr_below_threshold",
        "invalid_rr": "rr_below_threshold",
        "higher-timeframe counter-trend setup disabled": "htf_countertrend",
    }
    rejection_reasons = {}
    for row in logs:
        if row.event_type != "candidate_rejected":
            continue
        raw = str((row.context_json or {}).get("reason", "unspecified"))
        reason = aliases.get(raw, raw.replace(" ", "_"))
        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    orders = list(db.scalars(select(ExecutionOrder).where(ExecutionOrder.submitted_at >= since)))
    return {
        "period": period,
        "closed_candles_processed": counts["closed_candle_processed"],
        "strategy_evaluations": counts["strategy_evaluation"],
        "candidates_generated": counts["candidate_generated"],
        "candidates_rejected": counts["candidate_rejected"],
        "setups_persisted": counts["setup_persisted"],
        "execution_eligible": counts["execution_eligible"],
        "orders_submitted": len(orders),
        "orders_filled": sum(row.status in {"FILLED", "filled"} for row in orders),
        "rejection_reasons": rejection_reasons,
    }


@router.get("/bot/strategies")
def strategies(db: Session = Depends(get_db)):
    row = state(db)
    return [
        {
            "id": x,
            "enabled": x in row.enabled_strategies_json,
            **row.strategy_config_json.get(x, {}),
        }
        for x in STRATEGIES
    ]


@router.put("/bot/strategies")
def save_strategies(
    body: dict, db: Session = Depends(get_db), current=Depends(require("admin"))
):
    row = state(db)
    enabled = body.get("enabled", [])
    if not set(enabled) <= set(STRATEGIES):
        raise HTTPException(422, "Unsupported strategy")
    row.enabled_strategies_json = enabled
    row.strategy_config_json = body.get("configuration", {})
    db.commit()
    event(db, "strategy_config_updated", f"Strategies updated by {current}")
    return clean(row)


@router.get("/bot/strategies/performance")
def strategy_performance():
    return []


def cipher():
    return credential_cipher(settings)


class Credentials(BaseModel):
    environment: str = Field(pattern="^(testnet|production)$")
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=8, max_length=256)
    label: str | None = Field(default=None, max_length=64)


@router.post("/binance/credentials")
@router.put("/binance/credentials")
async def credentials(
    body: Credentials, db: Session = Depends(get_db), current=Depends(require("admin"))
):
    if body.environment == "production":
        raise HTTPException(423, "Production credentials remain locked")
    f = cipher()
    row = db.scalar(
        select(ExchangeAccount).where(ExchangeAccount.environment == body.environment)
    ) or ExchangeAccount(environment=body.environment)
    row.encrypted_api_key = f.encrypt(body.api_key.encode()).decode()
    row.encrypted_api_secret = f.encrypt(body.api_secret.encode()).decode()
    row.masked_api_key = "*" * max(8, len(body.api_key) - 4) + body.api_key[-4:]
    row.label = body.label
    row.status = "saved"
    db.add(row)
    db.commit()
    await user_stream.restart()
    event(
        db,
        "binance_credentials_saved",
        f"Binance {body.environment} credentials updated by {current}",
    )
    return {
        "saved": True,
        "environment": row.environment,
        "masked_api_key": row.masked_api_key,
        "label": row.label,
    }


@router.delete("/binance/credentials")
async def delete_credentials(
    db: Session = Depends(get_db), current=Depends(require("admin"))
):
    row = db.scalar(select(ExchangeAccount).order_by(ExchangeAccount.id.desc()))
    if row:
        row.encrypted_api_key = None
        row.encrypted_api_secret = None
        row.masked_api_key = ""
        row.status = "disconnected"
        db.commit()
    await user_stream.stop()
    event(
        db,
        "binance_credentials_disconnected",
        f"Binance credentials removed by {current}",
    )
    return {"disconnected": True}


def stored_settings(db):
    return load_stored_settings(db, settings)


@router.get("/binance/connection/status")
def connection_status(db: Session = Depends(get_db)):
    row = db.scalar(select(ExchangeAccount).order_by(ExchangeAccount.id.desc()))
    return {
        "connected": bool(row and row.status == "connected"),
        "credentials_saved": bool(
            row and row.encrypted_api_key and row.encrypted_api_secret
        ),
        "account_status": row.status if row else "disconnected",
        "environment": row.environment if row else "testnet",
        "masked_api_key": row.masked_api_key if row else "",
        "label": row.label if row else None,
        "permissions": row.permissions_json if row else {},
        "last_tested_at": row.last_connected_at if row else None,
        "encryption_configured": bool(
            settings.credential_encryption_key
            and len(settings.credential_encryption_key) >= 32
        ),
    }


@router.post("/binance/connection/test")
async def test_connection(
    db: Session = Depends(get_db), current=Depends(require("admin"))
):
    row, cfg = stored_settings(db)
    client = BinanceSpotClient(cfg)
    started = time.perf_counter()
    try:
        drift = await client.sync_time()
        data = await client.account()
        row.status = "connected"
        row.account_type = data.get("accountType", "SPOT")
        row.permissions_json = {
            "canTrade": data.get("canTrade"),
            "canWithdraw": data.get("canWithdraw"),
            "canDeposit": data.get("canDeposit"),
        }
        row.last_connected_at = datetime.now(timezone.utc)
        row.last_error = None
        db.commit()
        return {
            "connected": True,
            "environment": row.environment,
            "account_type": row.account_type,
            "can_read_balances": True,
            "spot_trading": data.get("canTrade", False),
            "withdrawal": data.get("canWithdraw", False),
            "clock_drift_ms": drift,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "masked_api_key": row.masked_api_key,
            "last_tested_at": row.last_connected_at,
        }
    finally:
        await client.close()


@router.get("/binance/balances")
async def binance_balances(
    db: Session = Depends(get_db), current=Depends(require("admin", "trader", "viewer"))
):
    _, cfg = stored_settings(db)
    client = BinanceSpotClient(cfg)
    try:
        data = await client.account()
        return [
            {"asset": x["asset"], "available": x["free"], "locked": x["locked"]}
            for x in data.get("balances", [])
            if Decimal(x["free"]) + Decimal(x["locked"]) > 0
        ]
    finally:
        await client.close()


@router.get("/bot/trade-history")
def history(db: Session = Depends(get_db)):
    return [
        clean(x)
        for x in db.scalars(
            select(LivePosition)
            .where(LivePosition.status == "closed")
            .order_by(LivePosition.closed_at.desc())
        )
    ]
