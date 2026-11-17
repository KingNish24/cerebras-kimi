"""WebSocket endpoint implementing the DeepTutor turn protocol v2.

Wire contract: ``web/contracts/generated/turn-protocol.ts``.

Client commands handled:

* ``start_turn`` / ``message`` — launch an agent turn (content + attachments)
* ``subscribe_turn`` — replay a turn's event log after ``after_seq``
* ``subscribe_session`` — attach to the session's latest/active turn
* ``resume_from`` — alias of subscribe_turn for reconnect recovery
* ``unsubscribe`` — detach
* ``cancel_turn`` — cancel a running turn (acknowledged)
* ``regenerate`` — re-run the session's last input after truncating history
* ``submit_user_reply`` / ``user_input`` — acknowledged as unsupported (v1)
* ``check_active_turn`` — report the session's running turn, if any
* ``ping`` — liveness

Every stream event carries ``protocol_version``, ``seq`` (per-turn, dense,
1-based), ``session_id``, ``turn_id``, ``timestamp``, ``type``, ``source``,
``stage``, ``content``, ``metadata``. Terminal event is ``done``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.runtime import AgentRuntime

log = logging.getLogger("deeptutor.ws")

router = APIRouter()

PROTOCOL_VERSION = "2.0"
QUEUE_SIZE = 1000


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "protocol_error",
        "protocol_version": PROTOCOL_VERSION,
        "error_code": code,
        "message": message,
        "retryable": bool(extra.pop("retryable", False)),
        **extra,
    }


def _ack(command_type: str, command_id: str, turn_id: str, accepted: bool,
         error_code: str | None = None, message: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "command_ack",
        "protocol_version": PROTOCOL_VERSION,
        "command_id": command_id,
        "command_type": command_type,
        "accepted": accepted,
        "turn_id": turn_id,
    }
    if error_code:
        event["error_code"] = error_code
    if message:
        event["message"] = message
    return event


class Connection:
    """Per-socket state: command dispatch + turn subscriptions."""

    def __init__(self, websocket: WebSocket, runtime: AgentRuntime) -> None:
        self.ws = websocket
        self.runtime = runtime
        self.subscriptions: dict[str, asyncio.Queue] = {}  # turn_id -> queue
        self.tasks: set[asyncio.Task] = set()
        self.send_lock = asyncio.Lock()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send(self, event: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(event, ensure_ascii=False, default=str))

    async def pump(self, queue: asyncio.Queue, turn_id: str) -> None:
        """Forward events from a subscribed turn until DONE or disconnect."""
        try:
            while True:
                event = await queue.get()
                await self.send(event)
                if event.get("type") == "done":
                    return
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            return
        finally:
            self.subscriptions.pop(turn_id, None)

    def subscribe(self, turn_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        handle = self.runtime.get_turn(turn_id)
        if handle is not None:
            handle.subscribers.add(queue)
        self.subscriptions[turn_id] = queue
        return queue

    def unsubscribe(self, turn_id: str | None = None, session_id: str | None = None) -> None:
        if turn_id:
            self._drop(turn_id)
            return
        if session_id:
            for tid in list(self.subscriptions):
                handle = self.runtime.get_turn(tid)
                if handle and handle.session_id == session_id:
                    self._drop(tid)

    def _drop(self, turn_id: str) -> None:
        queue = self.subscriptions.pop(turn_id, None)
        handle = self.runtime.get_turn(turn_id)
        if handle and queue is not None:
            handle.subscribers.discard(queue)

    # -- command handlers ----------------------------------------------------

    async def handle(self, command: dict[str, Any]) -> None:
        ctype = command.get("type") or ("start_turn" if command.get("content") is not None else "")
        if ctype in ("start_turn", "message"):
            await self._start_turn(command)
        elif ctype == "subscribe_turn" or ctype == "resume_from":
            await self._subscribe_turn(command)
        elif ctype == "subscribe_session":
            await self._subscribe_session(command)
        elif ctype == "unsubscribe":
            self.unsubscribe(
                turn_id=command.get("turn_id"), session_id=command.get("session_id")
            )
        elif ctype == "cancel_turn":
            await self._cancel_turn(command)
        elif ctype == "regenerate":
            await self._regenerate(command)
        elif ctype in ("submit_user_reply", "user_input"):
            await self.send(
                _ack(
                    ctype,
                    str(command.get("command_id") or ""),
                    str(command.get("turn_id") or ""),
                    accepted=False,
                    error_code="unsupported",
                    message="This backend does not support mid-turn user input yet.",
                )
            )
        elif ctype == "check_active_turn":
            await self._check_active(command)
        elif ctype == "ping":
            await self.send({"type": "pong", "protocol_version": PROTOCOL_VERSION})
        else:
            await self.send(
                _error(
                    "unknown_message_type",
                    f"Unsupported command type: {ctype!r}",
                    session_id=command.get("session_id"),
                    turn_id=command.get("turn_id"),
                )
            )

    async def _start_turn(self, command: dict[str, Any]) -> None:
        handle, session_event, error = await self.runtime.start_turn(command)
        if error or handle is None:
            await self.send(
                _error(
                    "start_turn_rejected",
                    error or "failed to start turn",
                    session_id=command.get("session_id"),
                    retryable=False,
                )
            )
            return
        # Log the session-binding event for replay, deliver it directly, then
        # subscribe this socket to the turn's broadcast queue and pump until
        # done. (Emitting before subscribing avoids a duplicate delivery.)
        if session_event is not None:
            await self.runtime._emit(handle, session_event)
            await self.send(session_event)
        queue = self.subscribe(handle.turn_id)
        await self.pump(queue, handle.turn_id)

    async def _subscribe_turn(self, command: dict[str, Any]) -> None:
        turn_id = str(command.get("turn_id") or "")
        after_seq = int(command.get("after_seq") or 0)
        if not turn_id:
            await self.send(_error("invalid_command", "subscribe_turn requires turn_id"))
            return
        # Replay persisted/in-memory history first.
        for event in self.runtime.turn_events_after(turn_id, after_seq):
            await self.send(event)
        handle = self.runtime.get_turn(turn_id)
        if handle is not None and handle.status == "running":
            # Already replayed above; now follow live events.
            queue = self.subscribe(turn_id)
            self.spawn(self.pump(queue, turn_id))
        # A finished turn needs no live subscription — its log was replayed.

    async def _subscribe_session(self, command: dict[str, Any]) -> None:
        session_id = str(command.get("session_id") or "")
        after_seq = int(command.get("after_seq") or 0)
        if not session_id:
            await self.send(_error("invalid_command", "subscribe_session requires session_id"))
            return
        handle = self.runtime.active_turn_for_session(session_id)
        if handle is None:
            await self.send({
                "type": "active_turn_info",
                "protocol_version": PROTOCOL_VERSION,
                "status": "none",
            })
            return
        for event in self.runtime.turn_events_after(handle.turn_id, after_seq):
            await self.send(event)
        queue = self.subscribe(handle.turn_id)
        self.spawn(self.pump(queue, handle.turn_id))

    async def _cancel_turn(self, command: dict[str, Any]) -> None:
        command_id = str(command.get("command_id") or "")
        turn_id = str(command.get("turn_id") or "")
        handle = self.runtime.get_turn(turn_id)
        accepted = False
        if handle is not None and handle.status == "running":
            handle.cancel.set()
            accepted = True
        await self.send(
            _ack("cancel_turn", command_id, turn_id, accepted,
                 None if accepted else "turn_not_running",
                 None if accepted else "No running turn with that id.")
        )

    async def _regenerate(self, command: dict[str, Any]) -> None:
        session_id = str(command.get("session_id") or "")
        session = self.runtime._store.get(session_id)
        if session is None:
            await self.send(_error("start_turn_rejected", "session not found",
                                   session_id=session_id, retryable=False))
            return
        last_input = session.get("last_input")
        if not last_input:
            await self.send(_error("start_turn_rejected", "nothing to regenerate",
                                   session_id=session_id, retryable=False))
            return
        # Truncate vtx history back to (and including) the last user message,
        # then re-run with empty input — the model sees the same prompt.
        vtx_items = session.get("vtx_items", [])
        while vtx_items and vtx_items[-1].get("role") != "user":
            vtx_items.pop()
        if vtx_items:
            vtx_items.pop()  # drop the user item; start_turn re-adds it
        self.runtime._store.save(session)
        await self._start_turn({
            "type": "start_turn",
            "protocol_version": PROTOCOL_VERSION,
            "content": last_input,
            "session_id": session_id,
            "regenerate": True,
        })

    async def _check_active(self, command: dict[str, Any]) -> None:
        session_id = str(command.get("session_id") or "")
        handle = self.runtime.active_turn_for_session(session_id)
        if handle is None:
            await self.send({
                "type": "active_turn_info",
                "protocol_version": PROTOCOL_VERSION,
                "status": "none",
            })
            return
        await self.send({
            "type": "active_turn_info",
            "protocol_version": PROTOCOL_VERSION,
            "turn_id": handle.turn_id,
            "status": "running",
        })


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    runtime: AgentRuntime = websocket.app.state.runtime
    conn = Connection(websocket, runtime)
    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                break
            text = raw.get("text")
            if text is None:
                continue
            stripped = text.strip()
            if not stripped or stripped == "heartbeat":
                continue
            try:
                command = json.loads(stripped)
            except json.JSONDecodeError:
                await conn.send(_error("invalid_json", "Malformed JSON frame"))
                continue
            if not isinstance(command, dict):
                await conn.send(_error("invalid_command", "Command must be a JSON object"))
                continue
            version = command.get("protocol_version")
            if version is not None and version != PROTOCOL_VERSION:
                await conn.send(
                    _error(
                        "unsupported_protocol_version",
                        f"Unsupported protocol_version: {version!r}",
                        retryable=False,
                    )
                )
                continue
            # _start_turn blocks until the turn's done event; run handlers in
            # tasks so other commands (cancel, ping) interleave correctly.
            if (command.get("type") in ("start_turn", "message")
                    or (command.get("type") is None and command.get("content") is not None)):
                # _start_turn blocks until the turn's done event; run it as a
                # task so cancel/ping/subscribe commands interleave correctly.
                conn.spawn(conn.handle(command))
            else:
                await conn.handle(command)
    except WebSocketDisconnect:
        pass
    finally:
        for task in list(conn.tasks):
            task.cancel()
        for turn_id in list(conn.subscriptions):
            conn.unsubscribe(turn_id)
