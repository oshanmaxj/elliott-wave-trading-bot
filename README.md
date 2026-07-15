# WaveScope Smart Money Concepts + Elliott Wave Platform — Phase 4

A production-oriented, paper-analysis platform for Binance USDT perpetual futures. It ingests public BTCUSDT and ETHUSDT candles, builds causal SMC structure and liquidity context, and deterministically ranks Elliott impulse and zigzag counts from confirmed stored swings.

This release cannot place live orders, withdraw funds, or use authenticated Binance endpoints. It does not request API credentials.

## Architecture

```text
Binance public REST ──> historical sync/gap repair ──> PostgreSQL
Binance public WS   ──> candle upsert ──> close pipeline ──> PostgreSQL
                                           │
                                           ├─ indicators
                                           ├─ confirmed swings
                                           ├─ BOS / CHoCH
                                           ├─ FVG lifecycle
                                           └─ analysis snapshot
                                                    │
FastAPI REST + WebSocket <──────────────────────────┘
             │
             └─> React + TradingView Lightweight Charts
```

The backend is split into configuration, database models, repositories, market data clients, detectors, analysis services, and API modules. Every persistence step has a natural uniqueness key, making repeated candle updates and repeated close processing idempotent. PostgreSQL uses `NUMERIC` for all price and volume fields.

## Quick start with Docker

Prerequisites: Docker Desktop with Compose.

```bash
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
docker compose up --build
```

The `.env` file is optional for the default Docker configuration, but copying it is recommended before changing settings.

- Dashboard: http://localhost:5173
- Backend: http://localhost:8000
- Interactive API docs: http://localhost:8000/docs
- OpenAPI schema: http://localhost:8000/openapi.json

On startup, Compose waits for PostgreSQL and Redis, the backend runs Alembic migrations, initializes configured symbols, starts historical synchronization in the background, and connects to Binance combined kline streams.

Stop services with `docker compose down`. To also remove persisted development data, explicitly run `docker compose down -v`.

## Environment variables

| Variable | Purpose | Default/example |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy PostgreSQL connection | `postgresql+psycopg://elliott:elliott@postgres:5432/elliott_wave` |
| `REDIS_URL` | Redis connection for infrastructure expansion | `redis://redis:6379/0` |
| `BINANCE_REST_BASE_URL` | Public futures REST origin | `https://fapi.binance.com` |
| `BINANCE_WS_BASE_URL` | Public combined stream origin | `wss://fstream.binance.com/stream` |
| `DEFAULT_SYMBOLS` | Allowed enabled symbols | `BTCUSDT,ETHUSDT` |
| `DEFAULT_TIMEFRAMES` | Enabled intervals | `15m,1h,4h` |
| `HISTORICAL_CANDLE_LIMIT` | Initial candle target per stream | `500` |
| `LOG_LEVEL` | Application log level | `INFO` |
| `API_HOST` / `API_PORT` | Backend bind address | `0.0.0.0` / `8000` |
| `FRONTEND_URL` | Exact CORS origin | `http://localhost:5173` |
| `ENABLE_STARTUP_SYNC` | Run initial REST sync | `true` |
| `ANALYZE_HISTORICAL_CANDLES` | Analyze closed candles after historical sync | `true` |
| `ENABLE_MARKET_STREAM` | Run live public WebSocket | `true` |

Only supported symbols and timeframes pass validation. Runtime analysis settings are available at `GET/PUT /api/settings` and stored in PostgreSQL after the first update.

## Database and migrations

Run migrations inside the service:

```bash
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend alembic current
docker compose run --rm backend alembic downgrade -1
```

Create a future migration after model changes:

```bash
docker compose run --rm backend alembic revision --autogenerate -m "describe change"
```

### Tables

| Table | Responsibility |
|---|---|
| `symbols` | Exchange instruments and activation state |
| `candles` | Idempotent OHLCV/kline storage by symbol, timeframe and open time |
| `swing_points` | Confirmed high/low pivots with strength and detector metadata |
| `market_structure_events` | Deduplicated BOS/CHoCH breaks and trend transitions |
| `fvg_zones` | Three-candle imbalance bounds and mitigation lifecycle |
| `analysis_snapshots` | Candle-close trend, latest event, indicators and confidence |
| `liquidity_pools` | Equal-high/equal-low buy-side and sell-side liquidity with sweep state |
| `order_blocks` | BOS-derived institutional candle zones and mitigation lifecycle |
| `alerts` | Deduplicated structure, FVG, liquidity-sweep and order-block alerts |
| `bot_logs` | Structured operational and failure logs |
| `settings` | Validated runtime detector configuration |
| `liquidity_sweeps` / `trade_setups` | Causal sweep lifecycle and paper-only opportunities |
| `elliott_wave_counts` | Ranked primary, alternate, completed and invalidated deterministic counts |
| `elliott_wave_points` | Confirmed swing-backed 0–5 and A–C points with Fibonacci ratios and durations |

## Historical synchronization

Startup automatically synchronizes all six configured symbol/timeframe streams. Trigger a bounded synchronization manually:

```bash
curl -X POST http://localhost:8000/api/market-data/sync \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTCUSDT","timeframe":"1h","start_time":"2026-07-01T00:00:00Z","end_time":"2026-07-07T00:00:00Z"}'
```

The response reports fetched, created, updated, detected-gap, and backfilled counts. Binance errors and rate limits use bounded exponential-backoff retries. Existing closed candles are not downgraded by stale open updates.

Run or rebuild analysis manually, and inspect live progress:

```bash
curl -X POST http://localhost:8000/api/analysis/backfill \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTCUSDT","timeframe":"1h","start_time":null,"end_time":null,"limit":500,"rebuild":false}'
curl http://localhost:8000/api/analysis/backfill/status
```

Set `limit` to `null` to process every selected closed candle. `rebuild: true` deletes derived analysis only for the requested symbol/timeframe before chronological replay; candles are never deleted.

## Smart Money Concepts APIs

```text
GET /api/liquidity?symbol=BTCUSDT&timeframe=1h
GET /api/order-blocks?symbol=BTCUSDT&timeframe=1h
GET /api/premium-discount?symbol=BTCUSDT&timeframe=1h
GET /api/market-bias?symbol=BTCUSDT
GET /api/structure-score?symbol=BTCUSDT&timeframe=1h
GET /api/alerts?symbol=BTCUSDT&timeframe=1h
```

Liquidity tolerance is configurable through `GET/PUT /api/settings`. Phase 2 analysis remains candle-close causal and is included in historical backfill and live WebSocket processing.

## Liquidity sweeps and paper setups

WaveScope detects candidate and confirmed liquidity reclaims, failed sweeps, and deterministic paper-analysis setups. No endpoint submits an exchange order.

```text
GET  /api/liquidity-sweeps
GET  /api/liquidity-sweeps/{id}
GET  /api/trade-setups
GET  /api/trade-setups/summary
GET  /api/trade-setups/{id}
POST /api/trade-setups/{id}/reject
POST /api/trade-setups/{id}/paper-trade  # returns 409 until a paper executor exists
```

Run migration `0003` before deploying this feature. The current historical replay marker is version 4, and rebuild mode deletes derived sweep/setup/wave records without deleting candles.

## Elliott Wave engine

Run migration `0004` before deploying the Elliott engine. The first release supports bullish/bearish impulses and ABC zigzags, stores multiple valid candidates, enforces hard structural invalidation, and scores Fibonacci, structure, higher-timeframe, liquidity, zone, momentum and freshness confluence. A conflicting 4h direction is penalized; it cannot override a hard rule failure.

```text
GET  /api/elliott-wave/counts
GET  /api/elliott-wave/counts/{id}
GET  /api/elliott-wave/latest?symbol=BTCUSDT&timeframe=1h
GET  /api/elliott-wave/context?symbol=BTCUSDT
POST /api/elliott-wave/recalculate
```

`POST /api/elliott-wave/recalculate` accepts `{"symbol":"BTCUSDT","timeframe":"1h","rebuild":false}`. Replay only exposes swings after their stored confirmation time, preventing future pivots from leaking into historical counts. Wave-derived paper setups reference `elliott_wave_count_id`; no endpoint can submit a live order.

## Local development without Compose

Start PostgreSQL and Redis, then set `DATABASE_URL` and `REDIS_URL` to localhost addresses.

```bash
cd backend
python -m venv .venv
# PowerShell: .venv\Scripts\Activate.ps1
# bash: source .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` and `/ws` to the local backend.

## Tests and verification

Backend tests use isolated SQLite databases and synthetic candles; they do not call Binance:

```bash
docker compose run --rm -e ENABLE_STARTUP_SYNC=false -e ENABLE_MARKET_STREAM=false backend pytest -q
# or locally: cd backend && pytest -q
```

Frontend production build:

```bash
docker compose build frontend
# or locally: cd frontend && npm install && npm test && npm run build
```

Validate the Compose file with `docker compose config`.

## Manual testing checklist

1. Run `docker compose up --build` and wait for all four services to become healthy.
2. Open `/api/symbols`; confirm BTCUSDT and ETHUSDT.
3. Open `/api/candles?symbol=BTCUSDT&timeframe=1h&limit=10`; confirm ordered, fixed-precision candle data.
4. Check `/api/market-data/status`; confirm six combined streams (two symbols × three timeframes), a recent message time, and `connected: true`.
5. Inspect PostgreSQL and confirm candles are unique by symbol/timeframe/open time.
6. Open Market Analysis, switch symbols/timeframes, and confirm candles, EMAs, swing markers, BOS/CHoCH markers, and FVG bounds load from the backend.
7. Keep the dashboard open through a candle update and confirm the live indicator remains online and data refreshes at candle close.
8. Inspect `/api/swings`, `/api/structure`, `/api/fvg`, and `/api/analysis/latest` after enough history has processed.
9. Update detector values on Settings, reload, and confirm they persisted.
10. Open System Logs and confirm REST sync, stream connection, and API activity.
11. Repeat the same sync request and confirm records are updated rather than duplicated.
12. Search the API schema and source for order/withdrawal endpoints; none are present.

## Troubleshooting

- **Backend remains unhealthy:** run `docker compose logs backend postgres`; verify the database health check and `DATABASE_URL`.
- **No initial candles:** Binance may be unavailable or region-restricted. Check System Logs/backend logs and the configured public futures base URL.
- **WebSocket reconnecting:** verify outbound `wss` access. The manager retries with exponential backoff and reports its state at `/api/market-data/status`.
- **No BOS/CHoCH yet:** events require confirmed protected swings and a close beyond the relevant level. This deliberately filters minor and wick-only breaks.
- **No FVG zones:** default filters require a displacement body and a gap of at least `0.15 × ATR`. Adjust settings if appropriate.
- **CORS errors in local development:** set `FRONTEND_URL` to the exact browser origin and restart the backend.
- **Resetting development state:** `docker compose down -v` removes both database and Redis volumes; this is destructive.

## Known limitations

- The initial Elliott pattern library covers standard impulses and ABC zigzags. Diagonals, flats, expanded flats and triangles are schema-ready but remain a later engine extension.
- Wave confidence is a deterministic confluence score, not a trained probability or profit guarantee.
- There is no order simulator, portfolio ledger, strategy executor, or live trading capability.
- Runtime symbol/timeframe changes affect analytical settings immediately, while changing upstream stream subscriptions requires a backend restart and environment update.
- The in-process WebSocket broadcaster is suitable for one backend replica. Redis-backed pub/sub is needed before horizontal scaling.
- FVG bounds are rendered as dashed horizontal zone boundaries rather than filled chart primitives.
- Wave-5 setups expose a reduced risk factor for a future paper position-sizing ledger; this phase does not size positions.

## Recommended next phase

1. Extend the deterministic pattern library with diagonals, flats, expanded flats and triangles.
2. Add Redis pub/sub and a dedicated durable worker queue for horizontally scalable candle-close processing.
3. Build a paper-trading ledger, fills/slippage model, portfolio accounting, position sizing, and drawdown controls.
4. Add multi-timeframe setup ranking using 4h bias, 1h structure, and 15m confirmation.
5. Add walk-forward backtesting, dataset/version tracking, feature provenance, and model calibration.
6. Add alerting, metrics, traces, retention policies, backups, and deployment manifests.
7. Add authenticated users and role-based controls before any future exchange execution work.
