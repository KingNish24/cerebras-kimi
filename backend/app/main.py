"""FastAPI app — REST API + WebSocket transport (no RPC).

REST: the handler registry in ``app/api/handlers.py`` mounted at /api/*.
WS: ``/ws`` carries the turn protocol and ``/ws/{channel}`` the legacy
feature channels (books, quiz judge, mastery, knowledge progress) — both
bridge into the same ``AcpAgent`` engine that used to sit behind stdio ACP.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .acp import AcpAgent
from .agent.runtime import AgentRuntime
from .api.handlers import router
from .config import get_settings
from .store.kb_store import KnowledgeBaseStore
from .store.session_store import SessionStore

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.workspace_dir.mkdir(parents=True, exist_ok=True)
settings.files_dir.mkdir(parents=True, exist_ok=True)

store = SessionStore(settings)
runtime = AgentRuntime(settings, store)
kb = KnowledgeBaseStore(settings)

app = FastAPI(title="DeepTutor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.settings = settings
app.state.store = store
app.state.runtime = runtime
app.state.kb = kb
app.include_router(router)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


def _ws_send(raw_event: bool = False):
    """Build the outbound translator for one socket.

    ACP envelopes are unwrapped to the raw protocol events the frontend
    turn client consumes: ``turn.event`` -> its params, ``session/update``
    -> ``{"type":"session_update", ...}``, ``legacy.event`` -> the inner
    legacy payload. JSON-RPC responses pass through untouched.
    """

    def make(ws: WebSocket, channel: str):
        async def send(msg: dict[str, Any]) -> None:
            method = msg.get("method")
            if method == "turn.event":
                await ws.send_json(msg.get("params") or {})
            elif method == "session/update":
                params = msg.get("params") or {}
                await ws.send_json(
                    {"type": "session_update", "session_id": params.get("sessionId"),
                     "update": params.get("update")}
                )
            elif method == "legacy.event":
                params = msg.get("params") or {}
                data = params.get("data")
                try:
                    payload = json.loads(data) if isinstance(data, str) else data
                except Exception:  # noqa: BLE001
                    payload = data
                await ws.send_json(payload if isinstance(payload, dict) else {"data": payload})
            elif "id" in msg and channel:
                return  # channel sockets speak the legacy protocol, no ids
            else:
                await ws.send_json(msg)

        return send

    return make


async def _ws_loop(ws: WebSocket, channel: str) -> None:
    await ws.accept()
    app_state = SimpleNamespace(state=app.state)
    agent = AcpAgent(store=store, runtime=runtime, app=app_state,
                     send=_ws_send()(ws, channel))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if channel:
                # Legacy feature channel: wrap the raw client message as a
                # legacy.send call so acp.py's channel handlers reply.
                await agent.handle({
                    "jsonrpc": "2.0", "id": None,
                    "method": "legacy.send",
                    "params": {"channel": channel, "message": msg},
                })
            else:
                await agent.handle(msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass


@app.websocket("/ws")
async def ws_main(ws: WebSocket) -> None:
    await _ws_loop(ws, "")


@app.websocket("/ws/{channel:path}")
async def ws_channel(ws: WebSocket, channel: str) -> None:
    await _ws_loop(ws, channel)


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8001, reload=False)


if __name__ == "__main__":
    main()
