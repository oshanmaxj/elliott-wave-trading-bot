from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.auth import current_user, require_roles
from app.database.session import get_db
from app.execution.binance import (
    BinanceCredentialService,
    BinanceError,
    BinanceSpotClient,
)
from app.execution.service import ExecutionRiskEngine, client_order_id, setup_fingerprint
from app.models import (
    BotRuntimeState,
    DailyRiskLedger,
    ExchangeAccount,
    ExecutionEvent,
    ExecutionOrder,
    LivePosition,
    ProtectiveOrder,
    Symbol,
    TradeSetup,
)

router = APIRouter(
    prefix="/api/execution", tags=["execution"], dependencies=[Depends(current_user)]
)
settings = get_settings()


admin = require_roles("admin")


def kill(db):
    row = db.scalar(
        select(DailyRiskLedger)
        .where(
            DailyRiskLedger.exchange == "binance",
            DailyRiskLedger.environment == settings.binance_environment,
        )
        .order_by(DailyRiskLedger.created_at.desc())
    )
    return bool(row and row.kill_switch_triggered)


def serialize(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    account = db.scalar(
        select(ExchangeAccount)
        .where(ExchangeAccount.environment == settings.binance_environment)
        .order_by(ExchangeAccount.id.desc())
    )
    return {
        "environment": settings.binance_environment,
        "mode": settings.execution_mode,
        "enabled": settings.binance_execution_enabled,
        "production_locked": not (
            settings.binance_execution_enabled
            and settings.binance_environment == "production"
            and settings.execution_mode == "live"
            and settings.allow_production_orders
        ),
        "connected": bool(account and account.status == "connected"),
        "masked_api_key": account.masked_api_key
        if account
        else BinanceCredentialService(
            settings.binance_api_key,
            settings.binance_api_secret,
            settings.binance_environment,
        ).masked_key,
        "kill_switch": kill(db),
    }


@router.post("/connect/test", dependencies=[Depends(admin)])
async def connect_test(db: Session = Depends(get_db)):
    client = BinanceSpotClient(settings)
    try:
        drift = await client.sync_time()
        if abs(drift) > settings.binance_max_clock_drift_ms:
            raise HTTPException(409, "Binance clock drift exceeds configured maximum")
        data = await client.account()
        permissions = {
            k: data.get(k)
            for k in ("canTrade", "canWithdraw", "canDeposit", "permissions")
        }
        cred = BinanceCredentialService(
            settings.binance_api_key,
            settings.binance_api_secret,
            settings.binance_environment,
        )
        row = db.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.environment == settings.binance_environment
            )
        ) or ExchangeAccount(environment=settings.binance_environment)
        row.masked_api_key = cred.masked_key
        row.status = "connected"
        row.permissions_json = permissions
        row.last_connected_at = datetime.now(timezone.utc)
        row.last_error = None
        db.add(row)
        db.commit()
        return {
            "success": True,
            "environment": settings.binance_environment,
            "permissions": permissions,
            "account_type": data.get("accountType", "SPOT"),
            "masked_key": cred.masked_key,
        }
    finally:
        await client.close()


async def context(db, setup_id):
    setup = db.scalar(
        select(TradeSetup).where(TradeSetup.id == setup_id).with_for_update()
    )
    if not setup:
        raise HTTPException(404, "Trade setup not found")
    symbol = db.get(Symbol, setup.symbol_id)
    client = BinanceSpotClient(settings)
    try:
        price = Decimal((await client.ticker_price(symbol.symbol))["price"])
        info = (await client.exchange_info(symbol.symbol))["symbols"][0]
        account = await client.account()
        return (
            setup,
            symbol,
            client,
            ExecutionRiskEngine(settings).evaluate(
                db, setup, symbol, account, price, info, kill_switch=kill(db)
            ),
        )
    except Exception:
        await client.close()
        raise


@router.post("/setups/{setup_id}/evaluate", dependencies=[Depends(admin)])
async def evaluate(setup_id: int, db: Session = Depends(get_db)):
    setup, symbol, client, decision = await context(db, setup_id)
    await client.close()
    return decision.json()


@router.post("/setups/{setup_id}/approve", dependencies=[Depends(admin)])
async def approve(setup_id: int, db: Session = Depends(get_db), current=Depends(admin)):
    setup = db.scalar(select(TradeSetup).where(TradeSetup.id == setup_id).with_for_update())
    if not setup:
        raise HTTPException(404, "Trade setup not found")
    runtime = db.scalar(select(BotRuntimeState).limit(1))
    if not runtime or not runtime.manual_approval_required:
        raise HTTPException(409, "Manual approval mode is not enabled")
    if setup.status not in {"ready", "eligible", "pending_approval"}:
        raise HTTPException(409, f"Setup is already {setup.status}")
    expires = setup.expires_at if setup.expires_at.tzinfo else setup.expires_at.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        setup.status = "expired"
        db.commit()
        raise HTTPException(409, "Setup has expired")
    setup.status = "approved"
    symbol = db.get(Symbol, setup.symbol_id)
    db.add(ExecutionEvent(severity="INFO", event_type="setup_approved", exchange="binance", environment="testnet", symbol_id=symbol.id, trade_setup_id=setup.id, message=f"Setup approved by {current}", metadata_json={"approved_by": current}))
    db.commit()
    from app.execution.orchestrator import AutomaticTestnetExecutor
    result = await AutomaticTestnetExecutor().handoff(setup_id, manual_approved=True)
    if not result.get("started"):
        raise HTTPException(409, {"message": "Approval risk checks failed", "reason": result.get("reason")})
    return {"approved": True, "setup_id": setup_id, "execution": result}


@router.post("/setups/{setup_id}/reject", dependencies=[Depends(admin)])
def reject(setup_id: int, body: dict | None = None, db: Session = Depends(get_db), current=Depends(admin)):
    setup = db.scalar(select(TradeSetup).where(TradeSetup.id == setup_id).with_for_update())
    if not setup:
        raise HTTPException(404, "Trade setup not found")
    if setup.status not in {"ready", "eligible", "pending_approval"}:
        raise HTTPException(409, f"Setup is already {setup.status}")
    setup.status = "rejected"
    reason = str((body or {}).get("reason") or "Rejected by administrator")[:500]
    symbol = db.get(Symbol, setup.symbol_id)
    db.add(ExecutionEvent(severity="INFO", event_type="setup_rejected", exchange="binance", environment="testnet", symbol_id=symbol.id, trade_setup_id=setup.id, message=reason, metadata_json={"rejected_by": current, "reason": reason, "rejected_at": datetime.now(timezone.utc).isoformat()}))
    db.commit()
    return {"rejected": True, "setup_id": setup_id, "reason": reason}


@router.post("/setups/{setup_id}/execute", dependencies=[Depends(admin)])
async def execute(setup_id: int, db: Session = Depends(get_db)):
    if settings.binance_environment != "testnet":
        raise HTTPException(423, "Production execution is locked in Phase 6")
    setup, symbol, client, decision = await context(db, setup_id)
    if settings.execution_require_manual_approval and setup.status != "approved":
        await client.close()
        raise HTTPException(409, "Manual approval required")
    if not decision.approved:
        await client.close()
        raise HTTPException(
            409, {"message": "Risk checks failed", "decision": decision.json()}
        )
    cid = client_order_id(settings.binance_environment, setup.id)
    order = ExecutionOrder(
        environment=settings.binance_environment,
        symbol_id=symbol.id,
        trade_setup_id=setup.id,
        client_order_id=cid,
        setup_fingerprint=setup_fingerprint(symbol.symbol, setup),
        side="BUY",
        order_type="MARKET",
        requested_quantity=decision.adjusted_quantity,
        status="submitting",
        execution_state="submitting",
        submitted_at=datetime.now(timezone.utc),
    )
    db.add(order)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        await client.close()
        raise HTTPException(409, "Duplicate execution prevented")
    params = {
        "symbol": symbol.symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": str(decision.adjusted_quantity),
        "newClientOrderId": cid,
    }
    try:
        await client.test_order(params)
        response = await client.place_order(params)
        order.exchange_order_id = str(response.get("orderId"))
        order.status = response.get("status", "NEW")
        order.execution_state = "acknowledged"
        order.acknowledged_at = datetime.now(timezone.utc)
        order.raw_status_json = response
        setup.status = "acknowledged"
        db.commit()
        return serialize(order)
    except BinanceError as exc:
        if exc.unknown:
            order.status = "execution_unknown"
            order.execution_state = "unknown"
            db.commit()
            try:
                found = await client.get_order(symbol.symbol, cid)
                order.raw_status_json = found
                order.exchange_order_id = str(found.get("orderId"))
                order.status = found.get("status", "UNKNOWN")
                order.execution_state = "reconciled"
                db.commit()
            except BinanceError:
                pass
            raise HTTPException(503, "Order state unknown; reconciliation required")
        order.status = "exchange_rejected"
        order.execution_state = "rejected"
        order.rejection_reason = str(exc)
        db.commit()
        raise HTTPException(409, "Binance rejected the order")
    finally:
        await client.close()


@router.get("/orders")
def orders(db: Session = Depends(get_db)):
    return [
        serialize(x)
        for x in db.scalars(
            select(ExecutionOrder).order_by(ExecutionOrder.id.desc()).limit(200)
        )
    ]


@router.get("/orders/{row_id}")
def order(row_id: int, db: Session = Depends(get_db)):
    row = db.get(ExecutionOrder, row_id)
    if not row:
        raise HTTPException(404, "Order not found")
    return serialize(row)


@router.get("/positions")
def positions(db: Session = Depends(get_db)):
    return [
        serialize(x)
        for x in db.scalars(select(LivePosition).order_by(LivePosition.id.desc()))
    ]


@router.get("/positions/{row_id}")
def position(row_id: int, db: Session = Depends(get_db)):
    row = db.get(LivePosition, row_id)
    if not row:
        raise HTTPException(404, "Position not found")
    return serialize(row)


@router.get("/positions/{row_id}/protection")
def position_protection(row_id: int, db: Session = Depends(get_db)):
    position = db.get(LivePosition, row_id)
    if not position:
        raise HTTPException(404, "Position not found")
    return [
        serialize(row)
        for row in db.scalars(
            select(ProtectiveOrder)
            .where(ProtectiveOrder.live_position_id == row_id)
            .order_by(ProtectiveOrder.id.desc())
        )
    ]


@router.post("/positions/{row_id}/protection/reconcile", dependencies=[Depends(admin)])
async def reconcile_position_protection(row_id: int):
    from app.execution.protection import spot_protection_service

    return await spot_protection_service.reconcile(row_id)


@router.get("/events")
def events(db: Session = Depends(get_db)):
    return [
        serialize(x)
        for x in db.scalars(
            select(ExecutionEvent).order_by(ExecutionEvent.id.desc()).limit(200)
        )
    ]


@router.get("/approval-queue")
def queue(db: Session = Depends(get_db)):
    runtime = db.scalar(select(BotRuntimeState).limit(1))
    if not runtime or not runtime.manual_approval_required:
        return []
    return [
        {**serialize(x), "symbol": db.get(Symbol, x.symbol_id).symbol}
        for x in db.scalars(
            select(TradeSetup)
            .where(
                TradeSetup.status.in_(
                    ["ready", "eligible", "pending_approval", "triggered"]
                )
            )
            .order_by(TradeSetup.detected_at.desc())
        )
    ]


@router.get("/risk")
def risk(db: Session = Depends(get_db)):
    return {
        "kill_switch": kill(db),
        "max_risk_per_trade_pct": str(settings.max_risk_per_trade_pct),
        "max_daily_loss_pct": str(settings.max_daily_loss_pct),
        "max_open_positions": settings.max_open_positions,
        "max_symbol_exposure_pct": str(settings.max_symbol_exposure_pct),
    }


@router.get("/account", dependencies=[Depends(admin)])
async def account():
    client = BinanceSpotClient(settings)
    try:
        data = await client.account()
        return {
            "account_type": data.get("accountType", "SPOT"),
            "can_trade": data.get("canTrade", False),
            "permissions": data.get("permissions", []),
            "environment": settings.binance_environment,
        }
    finally:
        await client.close()


@router.get("/balances", dependencies=[Depends(admin)])
async def balances():
    client = BinanceSpotClient(settings)
    try:
        data = await client.account()
        return [
            {"asset": x["asset"], "free": x["free"], "locked": x["locked"]}
            for x in data.get("balances", [])
            if Decimal(x["free"]) + Decimal(x["locked"]) > 0
        ]
    finally:
        await client.close()


@router.post("/orders/{row_id}/cancel", dependencies=[Depends(admin)])
async def cancel(row_id: int, db: Session = Depends(get_db)):
    row = db.get(ExecutionOrder, row_id)
    if not row:
        raise HTTPException(404, "Order not found")
    symbol = db.get(Symbol, row.symbol_id)
    client = BinanceSpotClient(settings)
    try:
        data = await client.cancel_order(symbol.symbol, row.client_order_id)
        row.status = data.get("status", "CANCELED")
        row.execution_state = "canceled"
        row.canceled_at = datetime.now(timezone.utc)
        row.raw_status_json = data
        db.commit()
        return serialize(row)
    finally:
        await client.close()


@router.post("/reconcile", dependencies=[Depends(admin)])
async def reconcile(db: Session = Depends(get_db)):
    rows = list(
        db.scalars(
            select(ExecutionOrder).where(
                ExecutionOrder.execution_state.in_(
                    ["unknown", "submitting", "acknowledged"]
                )
            )
        )
    )
    client = BinanceSpotClient(settings)
    count = 0
    try:
        for row in rows:
            symbol = db.get(Symbol, row.symbol_id)
            try:
                data = await client.get_order(symbol.symbol, row.client_order_id)
                row.raw_status_json = data
                row.status = data.get("status", "UNKNOWN")
                row.exchange_order_id = str(data.get("orderId"))
                row.executed_quantity = Decimal(data.get("executedQty", "0"))
                row.execution_state = "reconciled"
                count += 1
            except BinanceError as exc:
                if exc.code != -2013:
                    raise
        db.commit()
        return {"reconciled": count, "checked": len(rows)}
    finally:
        await client.close()


def set_kill(value, db):
    row = db.scalar(
        select(DailyRiskLedger)
        .where(
            DailyRiskLedger.exchange == "binance",
            DailyRiskLedger.environment == settings.binance_environment,
        )
        .order_by(DailyRiskLedger.created_at.desc())
    )
    now = datetime.now(timezone.utc)
    if not row:
        row = DailyRiskLedger(
            trading_date=now,
            exchange="binance",
            environment=settings.binance_environment,
            starting_equity=0,
            current_equity=0,
        )
    row.kill_switch_triggered = value
    db.add(row)
    db.commit()
    return {"kill_switch": value}


@router.post("/kill-switch/enable", dependencies=[Depends(admin)])
def enable_kill(db: Session = Depends(get_db)):
    return set_kill(True, db)


@router.post("/kill-switch/disable", dependencies=[Depends(admin)])
def disable_kill(db: Session = Depends(get_db)):
    return set_kill(False, db)
