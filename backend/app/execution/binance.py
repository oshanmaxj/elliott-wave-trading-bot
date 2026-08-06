import asyncio
import hashlib
import hmac
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
import httpx


class BinanceError(RuntimeError):
    def __init__(self, message: str, *, code=None, status=None, unknown=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.unknown = unknown


@dataclass
class BinanceCredentialService:
    api_key: str
    api_secret: str
    environment: str

    @property
    def masked_key(self):
        return (
            ("*" * max(0, len(self.api_key) - 4) + self.api_key[-4:])
            if self.api_key
            else ""
        )


class BinanceSpotClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.base_url = (
            settings.binance_testnet_base_url
            if settings.binance_environment == "testnet"
            else settings.binance_production_base_url
        )
        self.http = httpx.AsyncClient(
            base_url=self.base_url, timeout=10, transport=transport
        )
        self.time_offset_ms = 0
        self.used_weight = None

    def sign(self, params: dict[str, Any]) -> str:
        return hmac.new(
            self.settings.binance_api_secret.encode(),
            urlencode(params).encode(),
            hashlib.sha256,
        ).hexdigest()

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, *, params=None, signed=False, trading=False):
        values = {k: str(v) for k, v in (params or {}).items() if v is not None}
        if signed:
            values.update(
                timestamp=str(int(time.time() * 1000) + self.time_offset_ms),
                recvWindow=str(self.settings.binance_recv_window_ms),
            )
            values["signature"] = self.sign(values)
        headers = {"X-MBX-APIKEY": self.settings.binance_api_key} if signed else {}
        for attempt in range(3):
            try:
                response = await self.http.request(
                    method, path, params=values, headers=headers
                )
                self.used_weight = response.headers.get("x-mbx-used-weight-1m")
                if response.status_code in (418, 429):
                    if attempt == 2:
                        raise BinanceError(
                            "Binance rate limit", status=response.status_code
                        )
                    await asyncio.sleep(
                        min(int(response.headers.get("retry-after", "1")), 5)
                    )
                    continue
                if response.status_code >= 500:
                    raise BinanceError(
                        "Binance server response left execution state unknown",
                        status=response.status_code,
                        unknown=trading,
                    )
                data = response.json()
                if response.is_error:
                    raise BinanceError(
                        str(data.get("msg", "Binance rejected request")),
                        code=data.get("code"),
                        status=response.status_code,
                    )
                return data
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if trading:
                    raise BinanceError(
                        "Transport failure left execution state unknown", unknown=True
                    ) from exc
                if attempt == 2:
                    raise BinanceError("Binance unavailable") from exc
                await asyncio.sleep((2**attempt) * 0.1 + random.random() * 0.1)

    async def sync_time(self):
        started = int(time.time() * 1000)
        data = await self.request("GET", "/api/v3/time")
        ended = int(time.time() * 1000)
        self.time_offset_ms = int(data["serverTime"]) - ((started + ended) // 2)
        return self.time_offset_ms

    async def exchange_info(self, symbol=None):
        return await self.request(
            "GET", "/api/v3/exchangeInfo", params={"symbol": symbol} if symbol else {}
        )

    async def ticker_price(self, symbol):
        return await self.request(
            "GET", "/api/v3/ticker/price", params={"symbol": symbol}
        )

    async def account(self):
        return await self.request("GET", "/api/v3/account", signed=True)

    async def open_orders(self, symbol=None):
        return await self.request(
            "GET", "/api/v3/openOrders", params={"symbol": symbol}, signed=True
        )

    async def get_order(self, symbol, client_order_id):
        return await self.request(
            "GET",
            "/api/v3/order",
            params={"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
        )

    async def trades(self, symbol):
        return await self.request(
            "GET", "/api/v3/myTrades", params={"symbol": symbol}, signed=True
        )

    async def test_order(self, params):
        return await self.request(
            "POST", "/api/v3/order/test", params=params, signed=True, trading=True
        )

    async def place_order(self, params):
        return await self.request(
            "POST", "/api/v3/order", params=params, signed=True, trading=True
        )

    async def cancel_order(self, symbol, client_order_id):
        return await self.request(
            "DELETE",
            "/api/v3/order",
            params={"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
            trading=True,
        )
