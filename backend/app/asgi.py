"""Minimal ASGI application — no web framework.

Serves exactly two things:

* ``POST /api/rpc`` (alias ``/rpc``) — JSON-RPC 2.0 over HTTP
* ``WS /api/rpc/ws`` (aliases ``/ws/rpc``, ``/ws``) — JSON-RPC 2.0 over
  WebSocket for streaming turns

Run with any ASGI server::

    uvicorn app.main:app --host 0.0.0.0 --port 8001
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from .acp import AcpAgent

log = logging.getLogger("deeptutor.asgi")

RPC_PATHS = {"/api/rpc", "/rpc"}
WS_PATHS = {"/api/rpc/ws", "/ws/rpc", "/ws"}


class AcpApp:
    """ASGI callable owning the shared state handlers/agents need."""

    def __init__(self, state: Any) -> None:
        self.state = state

    # -- HTTP -----------------------------------------------------------------

    async def _http_rpc(self, receive, send) -> None:
        # Read the full request body.
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        try:
            payload = json.loads(body or b"null")
        except Exception:  # noqa: BLE001
            payload = None

        outbox: list[dict[str, Any]] = []

        async def emit(msg: dict[str, Any]) -> None:
            outbox.append(msg)

        agent = AcpAgent(
            store=self.state.store,
            runtime=self.state.runtime,
            app=self,
            send=emit,
        )
        messages = payload if isinstance(payload, list) else [payload]
        for msg in messages:
            await agent.handle(msg)

        responses = [m for m in outbox if isinstance(m, dict) and "id" in m]
        if isinstance(payload, list):
            result = outbox
        elif responses:
            result = responses[-1]
        elif outbox:
            result = outbox[-1]
        else:
            result = {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32603, "message": "No response"}}

        data = json.dumps(result, ensure_ascii=False).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": data})

    async def _http_404(self, send) -> None:
        data = b'{"detail":"Not Found"}'
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": data})

    # -- WebSocket ------------------------------------------------------------

    async def _ws(self, scope, receive, send) -> None:
        await send({"type": "websocket.accept"})

        async def emit(msg: dict[str, Any]) -> None:
            await send({
                "type": "websocket.send",
                "text": json.dumps(msg, ensure_ascii=False, default=str),
            })

        agent = AcpAgent(
            store=self.state.store,
            runtime=self.state.runtime,
            app=self,
            send=emit,
        )
        try:
            while True:
                msg = await receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                if text:
                    await agent.handle(text)
        except Exception:  # noqa: BLE001 - client gone
            pass
        finally:
            await agent.close()
        try:
            await send({"type": "websocket.close", "code": 1000})
        except Exception:  # noqa: BLE001
            pass

    # -- entry -----------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if scope.get("method") == "POST" and path in RPC_PATHS:
                await self._http_rpc(receive, send)
            elif scope.get("method") == "OPTIONS":
                await send({
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", b"*"),
                        (b"access-control-allow-methods", b"POST, OPTIONS"),
                        (b"access-control-allow-headers", b"*"),
                    ],
                })
                await send({"type": "http.response.body", "body": b""})
            else:
                await self._http_404(send)
        elif scope["type"] == "websocket":
            if scope.get("path") in WS_PATHS:
                await self._ws(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
