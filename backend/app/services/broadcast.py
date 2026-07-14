import asyncio
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.connections: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self.lock:
            self.connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self.lock:
            self.connections.discard(websocket)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        message = {"type": event_type, "data": payload}
        dead: list[WebSocket] = []
        for websocket in list(self.connections):
            try:
                await websocket.send_json(message)
            except Exception:
                dead.append(websocket)
        async with self.lock:
            for websocket in dead:
                self.connections.discard(websocket)


broadcaster = ConnectionManager()

