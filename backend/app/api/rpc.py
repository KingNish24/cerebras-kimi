"""Transport shims for the ACP agent (``app/acp.py``).

The RPC protocol itself is transport-agnostic — this module only adapts
HTTP POST bodies and WebSocket frames to ``AcpAgent.handle``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..acp import AcpAgent

log = logging.getLogger("deeptutor.rpc")

router = APIRouter()


async def _agent_for_http(request: Request) -> AcpAgent:
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    agent = AcpAgent(
        store=request.app.state.store,
        runtime=request.app.state.runtime,
        app=request.app,
        send=send,
    )
    request.state._acp_outbox = sent  # type: ignore[attr-defined]
    return agent


@router.post("/rpc")
@router.post("/api/rpc")
async def rpc_http(request: Request) -> Any:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"}}

    agent = await _agent_for_http(request)
    messages = payload if isinstance(payload, list) else [payload]
    for msg in messages:
        await agent.handle(msg)
    outbox: list[dict[str, Any]] = request.state._acp_outbox  # type: ignore[attr-defined]
    if isinstance(payload, list):
        return outbox
    # Unary request: the response is the last message; notifications (e.g.
    # session/update from an inline prompt pump) precede it.
    responses = [m for m in outbox if "id" in m]
    return responses[-1] if responses else outbox[-1] if outbox else {
        "jsonrpc": "2.0", "id": None,
        "error": {"code": -32603, "message": "No response"},
    }


async def _acp_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    async def send(msg: dict[str, Any]) -> None:
        await websocket.send_text(__import__("json").dumps(msg, ensure_ascii=False, default=str))

    agent = AcpAgent(
        store=websocket.app.state.store,
        runtime=websocket.app.state.runtime,
        app=websocket.app,
        send=send,
    )
    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                break
            text = raw.get("text")
            if text:
                await agent.handle(text)
    except WebSocketDisconnect:
        pass
    finally:
        await agent.close()


@router.websocket("/api/rpc/ws")
@router.websocket("/ws/rpc")
async def acp_websocket(websocket: WebSocket) -> None:
    await _acp_ws(websocket)
