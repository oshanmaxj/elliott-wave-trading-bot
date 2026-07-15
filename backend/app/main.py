import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router, ws_router
from app.core.config import get_settings
from app.core.logging import configure_logging, log_event
from app.database.session import SessionLocal
from app.market_data.binance_ws import market_stream
from app.repositories.market import ensure_symbol
from app.services.historical_sync import HistoricalSyncService

config = get_settings()
configure_logging(config.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: list[asyncio.Task] = []
    try:
        with SessionLocal.begin() as db:
            for symbol in config.default_symbols:
                ensure_symbol(db, symbol)
        if config.enable_startup_sync:
            tasks.append(
                asyncio.create_task(
                    HistoricalSyncService().sync_configured(), name="historical-sync"
                )
            )
        if config.enable_market_stream:
            tasks.append(asyncio.create_task(market_stream.run(), name="market-stream"))
        app.state.background_tasks = tasks
        yield
    finally:
        await market_stream.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="WaveScope Paper Market Intelligence",
    version="4.0.0",
    description="Deterministic SMC and Elliott Wave analysis with paper-only setups. No live order execution.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(ws_router)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    log_event(
        "INFO",
        "api",
        "request",
        f"{request.method} {request.url.path}",
        {
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log_event("ERROR", "api", "unhandled_error", str(exc), {"path": request.url.path})
    return JSONResponse(
        status_code=500, content={"detail": "An internal error occurred"}
    )
