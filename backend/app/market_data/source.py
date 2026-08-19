"""Validation for Binance Spot market-data origins."""

from urllib.parse import urlsplit


SPOT_REST_PRODUCTION_HOSTS = {
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
    "data-api.binance.vision",
}
SPOT_WS_PRODUCTION_HOSTS = {
    "stream.binance.com",
    "data-stream.binance.vision",
}
SPOT_REST_TESTNET_HOSTS = {"testnet.binance.vision"}
SPOT_WS_TESTNET_HOSTS = {"stream.testnet.binance.vision"}
FUTURES_HOST_PREFIXES = ("fapi.", "fstream.", "dapi.", "dstream.")


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def reject_futures_market_data_url(url: str) -> None:
    hostname = host(url)
    if hostname.startswith(FUTURES_HOST_PREFIXES):
        raise ValueError(
            f"Binance Spot market data cannot use Futures host {hostname!r}"
        )


def spot_market_source(rest_url: str, ws_url: str) -> str:
    rest_host, ws_host = host(rest_url), host(ws_url)
    reject_futures_market_data_url(rest_url)
    reject_futures_market_data_url(ws_url)
    if (
        rest_host in SPOT_REST_PRODUCTION_HOSTS
        and ws_host in SPOT_WS_PRODUCTION_HOSTS
    ):
        return "spot_production"
    if rest_host in SPOT_REST_TESTNET_HOSTS and ws_host in SPOT_WS_TESTNET_HOSTS:
        return "spot_testnet"
    raise ValueError(
        "BINANCE_REST_BASE_URL and BINANCE_WS_BASE_URL must identify the same "
        "Binance Spot market-data source"
    )
