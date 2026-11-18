"""WebSocket feed: real-time snapshots and alerts. Authenticated by a JWT sent as the first message."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def feed(ws: WebSocket) -> None:
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        ws.app.state.auth.decode(str(first.get("token", "")))
    except (asyncio.TimeoutError, HTTPException, ValueError, WebSocketDisconnect, AttributeError):
        with contextlib.suppress(Exception):
            await ws.close(code=4401)
        return
    runtime = ws.app.state.runtime
    queue = runtime.subscribe()
    try:
        await ws.send_json({"type": "snapshot", "data": runtime.snapshot()})
        while True:
            msg = await queue.get()
            await ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        runtime.unsubscribe(queue)
