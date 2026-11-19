"""REST + SSE transport for the turn and legacy-channel protocols.

The old ``/ws`` and ``/ws/{channel}`` WebSockets are gone. Each client owns a
small in-process session: commands arrive over ``POST /api/realtime/commands``
and the backend's outbound JSON-RPC notifications are delivered as named SSE
events on ``GET /api/realtime/events``.

The wire payloads are deliberately identical to what the WebSocket transport
emitted (``turn.event`` is unwrapped to its params, ``session/update`` is
mapped to ``{"type": "session_update", ...}``, ``legacy.event`` is unwrapped to
its inner payload), so the frontend's event parsers do not change.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..acp import AcpAgent

router = APIRouter(prefix="/api/realtime")

HEARTBEAT_SECONDS = 15
CLIENT_IDLE_SECONDS = 15 * 60
SWEEP_SECONDS = 60
MAX_QUEUE = 2000


# -- outbound translation --------------------------------------------------


def _to_sse(msg: dict[str, Any]) -> str | None:
    """One outbound JSON-RPC message -> one SSE frame (or None to skip)."""
    method = msg.get("method")
    if method == "turn.event":
        payload = msg.get("params") or {}
        event = "turn_event"
        event_id = None
        if isinstance(payload, dict):
            seq = payload.get("seq")
            if isinstance(seq, int):
                event_id = f"{payload.get('turn_id', '')}:{seq}"
    elif method == "session/update":
        params = msg.get("params") or {}
        payload = {
            "type": "session_update",
            "session_id": params.get("sessionId"),
            "update": params.get("update"),
        }
        event = "session_update"
        event_id = None
    elif method == "legacy.event":
        params = msg.get("params") or {}
        raw = params.get("data")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            payload = raw
        if not isinstance(payload, dict):
            payload = {"data": payload}
        event = "legacy_event"
        event_id = None
    elif method is not None:
        payload = msg
        event = "message"
        event_id = None
    else:
        # Plain JSON-RPC response: returned by the POST instead of the stream.
        return None

    lines = [f"event: {event}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append("data: " + json.dumps(payload, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def _frame(event: str, payload: dict[str, Any]) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )


# -- per-client session -----------------------------------------------------


class _ClientSession:
    """One browser client: an AcpAgent plus its outbound event queue."""

    def __init__(self, hub: "RealtimeHub", client_id: str) -> None:
        self.id = client_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE)
        self.responses: dict[str, asyncio.Queue] = {}
        self.last_seen = time.monotonic()
        self.agent = AcpAgent(
            store=hub.store,
            runtime=hub.runtime,
            app=hub.app_state,
            send=self.send,
        )

    async def send(self, msg: dict[str, Any]) -> None:
        """AcpAgent send callback: capture responses, stream notifications."""
        msg_id = msg.get("id")
        if "method" not in msg:
            if msg_id is not None:
                waiter = self.responses.get(str(msg_id))
                if waiter is not None:
                    try:
                        waiter.put_nowait(msg)
                    except asyncio.QueueFull:
                        pass
            return
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Never wedge a turn on a slow/stale SSE reader: drop the oldest
            # frame. The client's seq-based replay closes any resulting gap.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def submit(self, message: Any) -> dict[str, Any]:
        """Handle one inbound command; await a JSON-RPC response when it has an id."""
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except Exception:  # noqa: BLE001
                raise HTTPException(status_code=400, detail="Invalid JSON")

        waiter: asyncio.Queue | None = None
        internal_id: str | None = None
        if (
            isinstance(message, dict)
            and message.get("jsonrpc") == "2.0"
            and message.get("id") is not None
        ):
            internal_id = uuid.uuid4().hex
            message = dict(message)
            message["id"] = internal_id
            waiter = asyncio.Queue(maxsize=1)
            self.responses[internal_id] = waiter

        try:
            await self.agent.handle(message)
        finally:
            if internal_id is not None:
                self.responses.pop(internal_id, None)

        if waiter is None:
            # Native turn commands and legacy channel messages are fire-and-
            # forget; their replies arrive as SSE notifications.
            return {"accepted": True}
        try:
            response = await asyncio.wait_for(waiter.get(), timeout=120)
        except asyncio.TimeoutError:
            return {"accepted": True}
        if "error" in response:
            raise HTTPException(status_code=400, detail=response["error"])
        result = response.get("result")
        return result if isinstance(result, dict) else {"result": result}


class RealtimeHub:
    """Registry of live client sessions, shared by the REST routes."""

    def __init__(self, store: Any, runtime: Any, app_state: Any) -> None:
        self.store = store
        self.runtime = runtime
        self.app_state = app_state
        self._clients: dict[str, _ClientSession] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task | None = None

    def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        for client_id in list(self._clients):
            await self.release(client_id)

    async def get(self, client_id: str) -> _ClientSession:
        async with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                client = _ClientSession(self, client_id)
                self._clients[client_id] = client
            client.last_seen = time.monotonic()
            return client

    async def release(self, client_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(client_id, None)
        if client is not None:
            await client.agent.close()

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            now = time.monotonic()
            async with self._lock:
                stale = [
                    cid for cid, client in self._clients.items()
                    if now - client.last_seen > CLIENT_IDLE_SECONDS
                ]
            for client_id in stale:
                await self.release(client_id)


# -- routes -----------------------------------------------------------------


@router.post("/commands")
async def submit_command(request: Request) -> dict[str, Any]:
    """Accept one turn command or legacy channel message over HTTP."""
    client_id = request.query_params.get("client_id") or uuid.uuid4().hex
    channel = request.query_params.get("channel") or ""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    client = await client_for(request).get(client_id)
    if channel:
        body = {
            "jsonrpc": "2.0",
            "id": None,
            "method": "legacy.send",
            "params": {"channel": channel, "message": body},
        }
    return await client.submit(body)


@router.get("/events")
async def stream_events(request: Request) -> StreamingResponse:
    """Deliver the client's outbound messages as a named-event SSE stream."""
    client_id = request.query_params.get("client_id") or uuid.uuid4().hex
    client = await client_for(request).get(client_id)

    async def generate() -> AsyncIterator[str]:
        yield _frame("ready", {"client_id": client_id})
        last_beat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(client.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - last_beat >= HEARTBEAT_SECONDS:
                        yield ": ping\n\n"
                        last_beat = now
                    continue
                client.last_seen = time.monotonic()
                frame = _to_sse(msg)
                if frame is not None:
                    yield frame
        finally:
            # Keep the client registered: EventSource reconnects with the same
            # id, and an idle sweep reclaims clients that never come back.
            client.last_seen = time.monotonic()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/clients/{client_id}")
async def close_client(request: Request, client_id: str) -> dict[str, bool]:
    await client_for(request).release(client_id)
    return {"closed": True}


def client_for(request: Request) -> RealtimeHub:
    return request.app.state.realtime
