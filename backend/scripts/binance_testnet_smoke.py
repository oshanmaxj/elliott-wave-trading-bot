"""Manual connectivity and /order/test smoke test. Never submits a real order."""
import asyncio, os
from app.core.config import Settings
from app.execution.binance import BinanceSpotClient
async def main():
    if os.getenv("RUN_BINANCE_TESTNET_SMOKE") != "true": raise SystemExit("Set RUN_BINANCE_TESTNET_SMOKE=true explicitly")
    settings=Settings()
    if settings.binance_environment != "testnet": raise SystemExit("Testnet only")
    client=BinanceSpotClient(settings)
    try:
        print({"clock_drift_ms":await client.sync_time(),"account_type":(await client.account()).get("accountType","SPOT")})
        if os.getenv("RUN_BINANCE_ORDER_TEST") == "true": print(await client.test_order({"symbol":"BTCUSDT","side":"BUY","type":"MARKET","quoteOrderQty":"10","newClientOrderId":"wavescope-smoke-test"}))
    finally: await client.close()
if __name__ == "__main__": asyncio.run(main())
