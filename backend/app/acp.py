"""ACP agent core — transport-agnostic JSON-RPC 2.0, mirroring vtx's
``coding_agent/acp.py`` structure: ``_send`` / ``_respond`` / ``_error`` /
``_notify`` / ``_dispatch`` over plain dicts, sessions own their runtime
state, agent->client traffic is ``session/update`` notifications.

No web-framework types here — transports (HTTP POST, WebSocket) adapt
messages to :meth:`AcpAgent.handle` and drain :meth:`AcpAgent.send`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

log = logging.getLogger("deeptutor.acp")

PROTOCOL_VERSION = 1

JSON_RPC_PARSE_ERROR = -32700
JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601
JSON_RPC_INVALID_PARAMS = -32602
JSON_RPC_INTERNAL_ERROR = -32603

SendFn = Callable[[dict[str, Any]], Awaitable[None]]

ACP_METHODS = {
    "initialize",
    "authenticate",
    "session/new",
    "session/load",
    "session/list",
    "session/delete",
    "session/prompt",
    "session/cancel",
    "session/set_model",
    "session/set_mode",
    "system.list_methods",
    "quiz.judge",
    "book.stream",
    "progress.subscribe",
    "legacy.send",
    # Turn-protocol aliases (DeepTutor frontend compat)
    "turn.start",
    "turn.subscribe",
    "turn.subscribe_session",
    "turn.unsubscribe",
    "turn.cancel",
    "turn.regenerate",
    "turn.check_active",
}

TURN_METHODS = {
    "turn.start": "start_turn",
    "turn.subscribe": "subscribe_turn",
    "turn.subscribe_session": "subscribe_session",
    "turn.resume_from": "resume_from",
    "turn.unsubscribe": "unsubscribe",
    "turn.cancel": "cancel_turn",
    "turn.regenerate": "regenerate",
    "turn.check_active": "check_active_turn",
}


# -- wire helpers (same shapes as vtx acp.py) -----------------------------------


def _is_request(msg: Any) -> bool:
    return (
        isinstance(msg, dict)
        and msg.get("jsonrpc") == "2.0"
        and isinstance(msg.get("method"), str)
        and "id" in msg
    )


def _is_notification(msg: Any) -> bool:
    return (
        isinstance(msg, dict)
        and msg.get("jsonrpc") == "2.0"
        and isinstance(msg.get("method"), str)
        and "id" not in msg
    )


def _prompt_text(params: dict[str, Any]) -> str:
    """Extract text from an ACP ``prompt`` content-block array."""
    prompt = params.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts = []
        for block in prompt:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(params.get("content") or "")


def _session_summary(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "sessionId": session.get("id") or session.get("session_id"),
        "title": session.get("title") or "",
        "updatedAt": session.get("updated_at") or session.get("created_at"),
    }


# -- ACP session/update mapping -------------------------------------------------


def _acp_update(event: dict[str, Any]) -> dict[str, Any] | None:
    """Turn-protocol event -> ACP ``session/update`` payload (vtx shapes)."""
    etype = event.get("type")
    if etype == "content":
        return {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": str(event.get("content") or "")},
        }
    if etype == "thinking":
        return {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": str(event.get("content") or "")},
        }
    if etype == "tool_call":
        meta = event.get("metadata") or {}
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": str(meta.get("tool_call_id") or event.get("turn_id") or ""),
            "title": str(meta.get("tool_name") or "tool"),
            "kind": "other",
            "status": "pending",
            "rawInput": meta.get("arguments"),
        }
    if etype == "tool_result":
        meta = event.get("metadata") or {}
        return {
            "sessionUpdate": "tool_call_update",
            "toolCallId": str(meta.get("tool_call_id") or ""),
            "status": "failed" if meta.get("is_error") else "completed",
            "rawOutput": {"output": str(event.get("content") or "")[:4000]},
        }
    if etype == "result":
        usage = (event.get("metadata") or {}).get("usage") or {}
        if usage:
            return {
                "sessionUpdate": "usage_update",
                "used": usage.get("total_tokens") or usage.get("input_tokens"),
                "size": usage.get("context_window"),
            }
    return None


def _command_ack(command: dict[str, Any], turn_id: str, accepted: bool,
                 error_code: str | None = None, message: str | None = None) -> dict[str, Any]:
    """command_ack event — the client awaits this before marking a send."""
    event: dict[str, Any] = {
        "type": "command_ack",
        "protocol_version": "2.0",
        "command_id": str(command.get("command_id") or ""),
        "command_type": str(command.get("type") or ""),
        "accepted": accepted,
        "turn_id": turn_id,
    }
    if error_code:
        event["error_code"] = error_code
    if message:
        event["message"] = message
    return event


class _Prompt:
    """One in-flight session/prompt: streams updates, resolves stopReason."""

    def __init__(self, session_id: str, turn_id: str, future: asyncio.Future) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.future = future


class AcpAgent:
    """Transport-agnostic ACP agent. ``send`` receives every outbound
    JSON-RPC message (responses and ``session/update`` notifications)."""

    def __init__(self, store: Any, runtime: Any, app: Any, send: SendFn) -> None:
        self.store = store
        self.runtime = runtime
        self.app = app
        self._send = send
        self._send_lock = asyncio.Lock()
        self._prompts: dict[str, _Prompt] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- outbound ------------------------------------------------------------

    async def _emit(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._send(msg)

    async def _respond(self, req_id: Any, result: Any) -> None:
        await self._emit({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _error(self, req_id: Any, code: int, message: str) -> None:
        await self._emit(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        )

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._emit({"jsonrpc": "2.0", "method": method, "params": params})

    async def _update(self, session_id: str, update: dict[str, Any]) -> None:
        await self._notify("session/update", {"sessionId": session_id, "update": update})

    # -- inbound --------------------------------------------------------------

    async def handle(self, raw: str | dict[str, Any]) -> None:
        """One inbound JSON-RPC message (or batch list handled by caller)."""
        if isinstance(raw, str):
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                await self._error(None, JSON_RPC_PARSE_ERROR, "Parse error")
                return
        else:
            msg = raw

        if isinstance(msg, list):
            for m in msg:
                await self._route(m)
            return
        await self._route(msg)

    async def _route(self, msg: Any) -> None:
        # Legacy turn-protocol command envelope: {"type": "start_turn", ...}
        # — no jsonrpc/id/method fields, so check before validation.
        if isinstance(msg, dict) and msg.get("jsonrpc") is None and isinstance(msg.get("type"), str):
            self._spawn(self._turn_start(msg))
            return

        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            await self._error(
                msg.get("id") if isinstance(msg, dict) else None,
                JSON_RPC_INVALID_REQUEST,
                "Invalid JSON-RPC request",
            )
            return

        method = msg.get("method")
        params = msg.get("params") or {}
        req_id = msg.get("id")

        if not isinstance(method, str):
            await self._error(req_id, JSON_RPC_INVALID_REQUEST, "Missing method")
            return

        # turn.* JSON-RPC aliases -> native turn handlers
        ctype = TURN_METHODS.get(method)
        if ctype is not None:
            self._spawn(self._turn_start({"type": ctype, **params}))
            if req_id is not None:
                await self._respond(req_id, {"accepted": True, "command": ctype})
            return

        try:
            if method == "session/prompt":
                await self._session_prompt(req_id, params)
            elif method == "session/cancel":
                await self._session_cancel(params)
                if req_id is not None:
                    await self._respond(req_id, {})
            else:
                result = await self._dispatch(req_id, method, params)
                if req_id is not None:
                    await self._emit(result)
        except Exception as exc:  # noqa: BLE001
            log.exception("acp method %s failed", method)
            if req_id is not None:
                await self._error(req_id, JSON_RPC_INTERNAL_ERROR, str(exc))

    # -- turn.* commands (native; no ws.py dependency) -------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _ack(self, command: dict[str, Any], turn_id: str, accepted: bool,
                   error_code: str | None = None, message: str | None = None) -> None:
        await self._notify("turn.event",
                           _command_ack(command, turn_id, accepted, error_code, message))

    async def _turn_start(self, command: dict[str, Any]) -> None:
        ctype = command.get("type")
        if ctype in ("start_turn", "message"):
            handle, session_event, error = await self.runtime.start_turn(command)
            if error or handle is None:
                await self._ack(command, "", False, "start_turn_rejected",
                                error or "failed to start turn")
                return
            if session_event is not None:
                await self.runtime._emit(handle, session_event)
                await self._notify("turn.event", session_event)
            await self._ack(command, handle.turn_id, True)
            self._spawn(self._pump_raw(handle))
        elif ctype == "cancel_turn":
            turn_id = str(command.get("turn_id") or "")
            handle = self.runtime.get_turn(turn_id)
            accepted = handle is not None and handle.status == "running"
            if accepted:
                handle.cancel.set()
            await self._ack(command, turn_id, accepted,
                            None if accepted else "turn_not_running",
                            None if accepted else "No running turn with that id.")
        elif ctype == "regenerate":
            await self._turn_regenerate(command)
            await self._ack(command, str(command.get("turn_id") or ""), True)
        elif ctype == "subscribe_turn" or ctype == "resume_from":
            await self._turn_subscribe(command)
            await self._ack(command, str(command.get("turn_id") or ""), True)
        elif ctype == "subscribe_session":
            await self._turn_subscribe_session(command)
            await self._ack(command, str(command.get("turn_id") or ""), True)
        elif ctype == "unsubscribe":
            self._turn_unsubscribe(command)
            await self._ack(command, "", True)
        elif ctype == "check_active_turn":
            await self._turn_check_active(command)
            await self._ack(command, "", True)
        elif ctype == "ping":
            await self._notify("turn.event",
                               {"type": "pong", "protocol_version": "2.0"})
        elif ctype in ("submit_user_reply", "user_input"):
            await self._ack(command, str(command.get("turn_id") or ""), False,
                            "unsupported",
                            "This backend does not support mid-turn user input yet.")

    async def _turn_regenerate(self, command: dict[str, Any]) -> None:
        session_id = str(command.get("session_id") or "")
        session = self.store.get(session_id)
        if session is None:
            await self._error(None, JSON_RPC_INVALID_PARAMS, "session not found")
            return
        last_input = session.get("last_input")
        if not last_input:
            await self._error(None, JSON_RPC_INVALID_PARAMS, "nothing to regenerate")
            return
        vtx_items = session.get("vtx_items", [])
        while vtx_items and vtx_items[-1].get("role") != "user":
            vtx_items.pop()
        if vtx_items:
            vtx_items.pop()
        self.store.save(session)
        await self._turn_start({
            "type": "start_turn",
            "protocol_version": "2.0",
            "content": last_input,
            "session_id": session_id,
            "regenerate": True,
        })

    async def _turn_subscribe(self, command: dict[str, Any]) -> None:
        turn_id = str(command.get("turn_id") or "")
        after_seq = int(command.get("after_seq") or 0)
        if not turn_id:
            await self._error(None, JSON_RPC_INVALID_PARAMS, "turn_id required")
            return
        for event in self.runtime.turn_events_after(turn_id, after_seq):
            await self._notify("turn.event", event)
        handle = self.runtime.get_turn(turn_id)
        if handle is not None and handle.status == "running":
            self._spawn(self._follow_turn(handle))

    async def _turn_subscribe_session(self, command: dict[str, Any]) -> None:
        session_id = str(command.get("session_id") or "")
        after_seq = int(command.get("after_seq") or 0)
        if not session_id:
            await self._error(None, JSON_RPC_INVALID_PARAMS, "session_id required")
            return
        handle = self.runtime.active_turn_for_session(session_id)
        if handle is None:
            await self._notify("turn.event", {
                "type": "active_turn_info", "protocol_version": "2.0",
                "status": "none"})
            return
        for event in self.runtime.turn_events_after(handle.turn_id, after_seq):
            await self._notify("turn.event", event)
        self._spawn(self._follow_turn(handle))

    async def _follow_turn(self, handle: Any) -> None:
        """Forward a running turn's events to this connection until done."""
        queue = asyncio.Queue(maxsize=1000)
        handle.subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                await self._notify("turn.event", event)
                if event.get("type") == "done":
                    return
        finally:
            handle.subscribers.discard(queue)

    def _turn_unsubscribe(self, command: dict[str, Any]) -> None:
        # Per-connection queues are transient pump tasks; cancelling them is
        # handled by connection close. This is a no-op acknowledgement path.
        pass

    async def _turn_check_active(self, command: dict[str, Any]) -> None:
        session_id = str(command.get("session_id") or "")
        handle = self.runtime.active_turn_for_session(session_id)
        if handle is None:
            await self._notify("turn.event", {
                "type": "active_turn_info", "protocol_version": "2.0",
                "status": "none"})
            return
        await self._notify("turn.event", {
            "type": "active_turn_info", "protocol_version": "2.0",
            "turn_id": handle.turn_id, "status": "running"})

    async def _pump_raw(self, handle: Any) -> None:
        """Stream a turn's events as turn.event + session/update until done."""
        queue = asyncio.Queue(maxsize=1000)
        handle.subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                await self._notify("turn.event", event)
                upd = _acp_update(event)
                if upd is not None:
                    await self._update(handle.session_id, upd)
                if event.get("type") == "done":
                    return
        finally:
            handle.subscribers.discard(queue)

    # -- session/prompt ---------------------------------------------------------

    async def _pump_prompt(self, prompt: _Prompt) -> None:
        handle = self.runtime.get_turn(prompt.turn_id)
        if handle is None:
            if not prompt.future.done():
                prompt.future.set_result({"stopReason": "refusal"})
            return
        queue = asyncio.Queue(maxsize=1000)
        handle.subscribers.add(queue)
        stop_reason = "end_turn"
        try:
            while True:
                event = await queue.get()
                await self._notify("turn.event", event)
                upd = _acp_update(event)
                if upd is not None:
                    await self._update(prompt.session_id, upd)
                etype = event.get("type")
                if etype == "error":
                    stop_reason = "refusal"
                elif etype == "done":
                    status = event.get("status") or getattr(handle, "status", "")
                    if status == "cancelled":
                        stop_reason = "cancelled"
                    elif status == "failed":
                        stop_reason = "refusal"
                    break
        except asyncio.CancelledError:
            stop_reason = "cancelled"
        finally:
            handle.subscribers.discard(queue)
            self._prompts.pop(prompt.session_id, None)
            if not prompt.future.done():
                prompt.future.set_result({"stopReason": stop_reason})

    async def _session_prompt(self, req_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or params.get("session_id") or "")
        content = _prompt_text(params)
        if not session_id or not content:
            await self._error(req_id, JSON_RPC_INVALID_PARAMS,
                              "session/prompt requires sessionId and prompt")
            return
        if self.store.get(session_id) is None:
            await self._error(req_id, JSON_RPC_INVALID_PARAMS,
                              f"Unknown session: {session_id}")
            return

        command = {
            "type": "start_turn",
            "protocol_version": "2.0",
            "content": content,
            "session_id": session_id,
        }
        for key in ("llm_selection", "tools", "capability", "persona",
                    "language", "attachments"):
            if key in params:
                command[key] = params[key]

        handle, _session_event, error = await self.runtime.start_turn(command)
        if error or handle is None:
            await self._error(req_id, JSON_RPC_INVALID_PARAMS,
                              error or "failed to start turn")
            return

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        prompt = _Prompt(session_id, handle.turn_id, future)
        self._prompts[session_id] = prompt
        task = asyncio.create_task(self._pump_prompt(prompt))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        try:
            result = await future
        except asyncio.CancelledError:
            result = {"stopReason": "cancelled"}
        await self._respond(req_id, result)

    async def _session_cancel(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or params.get("session_id") or "")
        prompt = self._prompts.get(session_id)
        handle = self.runtime.active_turn_for_session(session_id)
        if handle is not None and handle.status == "running":
            handle.cancel.set()
        if prompt is not None and not prompt.future.done():
            prompt.future.set_result({"stopReason": "cancelled"})

    # -- unary dispatch ---------------------------------------------------------

    async def _dispatch(
        self, req_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        p = params or {}

        if method in ("initialize", "authenticate"):
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {
                            "image": False, "audio": False, "embeddedContext": True,
                        },
                        "mcpCapabilities": {"http": False, "sse": False},
                    },
                    "agentInfo": {"name": "deeptutor-vtx", "version": "0.1.0"},
                    "authMethods": [],
                },
            }

        if method == "session/new":
            session = self.store.create(preferences=p.get("preferences"))
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"sessionId": session.get("id") or session.get("session_id")}}

        if method == "session/list":
            sessions = self.store.list(limit=int(p.get("limit") or 50))
            rows = [_session_summary(s) for s in sessions]
            # Merge vtx-native sessions (~/.vtx/sessions/*/*.jsonl) so chats
            # created in the vtx CLI/TUI show up here too (vtx-vscode style).
            try:
                from vtx.agent.session import Session as VtxSession
                from vtx.core.paths import get_config_dir

                known_files = {
                    s.get("vtx_session_file") for s in sessions if s.get("vtx_session_file")
                }
                vtx_rows = []
                sdir = get_config_dir() / "sessions"
                if sdir.is_dir():
                    for path in sdir.glob("*/*.jsonl"):
                        try:
                            info = VtxSession.build_session_info(path)
                        except Exception:  # noqa: BLE001
                            continue
                        if info is None or str(path) in known_files:
                            continue
                        vtx_rows.append({
                            "sessionId": info.id,
                            "title": info.name or info.first_message or "",
                            "updatedAt": info.modified.isoformat() if info.modified else None,
                            "vtx": True,
                            "vtx_file": str(path),
                        })
                vtx_rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
                rows.extend(vtx_rows)
            except Exception:  # noqa: BLE001
                pass
            return {"jsonrpc": "2.0", "id": req_id, "result": {"sessions": rows}}

        if method == "session/load":
            session_id = str(p.get("sessionId") or p.get("session_id") or "")
            session = self.store.get(session_id)
            if session is None:
                # vtx-native session id: find its jsonl and bind a DeepTutor
                # record to it so turns continue the same vtx conversation.
                try:
                    from vtx.agent.session import Session as VtxSession
                    from vtx.core.paths import get_config_dir

                    sdir = get_config_dir() / "sessions"
                    for path in (sdir.glob("*/*.jsonl") if sdir.is_dir() else []):
                        try:
                            info = VtxSession.build_session_info(path)
                        except Exception:  # noqa: BLE001
                            continue
                        if info is not None and info.id == session_id:
                            session = self.store.create()
                            session["vtx_session_file"] = str(path)
                            session["title"] = info.name or info.first_message or ""
                            self.store.save(session)
                            session_id = session["id"]
                            break
                except Exception:  # noqa: BLE001
                    pass
            if session is None:
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": JSON_RPC_INVALID_PARAMS,
                                  "message": f"Unknown session: {session_id}"}}
            replay = []
            for m in (session.get("messages") or [])[-50:]:
                role = m.get("role")
                text = m.get("content")
                if isinstance(text, list):
                    text = "".join(str(c.get("text") or "")
                                   for c in text if isinstance(c, dict))
                if not text:
                    continue
                replay.append({
                    "sessionUpdate":
                        "user_message_chunk" if role == "user" else "agent_message_chunk",
                    "content": {"type": "text", "text": str(text)},
                })
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"sessionId": session_id, "replay": replay}}

        if method == "session/delete":
            session_id = str(p.get("sessionId") or p.get("session_id") or "")
            if not self.store.delete(session_id):
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": JSON_RPC_INVALID_PARAMS,
                                  "message": f"Unknown session: {session_id}"}}
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "session/set_model":
            model = str(p.get("modelId") or p.get("model") or "")
            provider = str(p.get("provider") or "")
            if not model:
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": JSON_RPC_INVALID_PARAMS,
                                  "message": "session/set_model requires modelId"}}
            try:
                from vtx.core.config import add_recent_model, set_last_selected

                set_last_selected(model_id=model, provider=provider or None,
                                  thinking_level=None)
                if provider:
                    add_recent_model(provider, model)
            except Exception:  # noqa: BLE001
                pass
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "session/set_mode":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}


        if method == "legacy.send":
            return await self._legacy_send(req_id, p)
        if method == "quiz.judge":
            return await self._quiz_judge(req_id, p)
        if method == "book.stream":
            return await self._book_stream(req_id, p)
        if method == "progress.subscribe":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"subscribed": True}}

        if method == "system.list_methods":
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"methods": sorted(ACP_METHODS)}}

        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": JSON_RPC_METHOD_NOT_FOUND,
                          "message": f"Unknown method: {method}"}}

    async def _legacy_send(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """WS-compat bridge: legacy /ws/* JSON protocols over RPC+SSE.

        params: {channel, message} where message is the raw JSON the old
        WebSocket client sent. Replies go out as ``legacy.event``
        notifications carrying {channel, data}.
        """
        channel = str(params.get("channel") or "")
        raw = params.get("message")
        try:
            msg = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:  # noqa: BLE001
            msg = {}

        async def reply(payload: dict[str, Any]) -> None:
            await self._notify("legacy.event",
                               {"channel": channel, "data": json.dumps(payload)})

        if "judge" in channel:
            grade = await self._judge_payload(msg)
            await reply({"type": "result", "data": grade})
            await reply({"type": "done"})
        elif "books" in channel:
            await reply({"type": "ack", "status": "ok"})
        elif "mastery" in channel or "progress" in channel:
            await reply({"type": "subscribed", "revision": 0})
        else:
            await reply({"type": "ack"})
        return {"jsonrpc": "2.0", "id": req_id, "result": {"accepted": True}}

    async def _judge_payload(self, msg: dict[str, Any]) -> dict[str, Any]:
        question = str(msg.get("question") or msg.get("prompt") or "")
        answer = str(msg.get("answer") or msg.get("submission") or "")
        reference = str(msg.get("reference") or msg.get("expected") or "")
        prompt = (
            "Grade this quiz answer. Reply with JSON: "
            '{"score":0-100,"correct":bool,"feedback":"..."}\n\n'
            f"Question: {question}\nAnswer: {answer}\n"
            + (f"Reference: {reference}\n" if reference else "")
        )
        try:
            from vtx.agent.sdk.runner import Runner
            from vtx.agent.sdk.sessions import InMemorySession
            from vtx.core.config import get_last_selected

            sel = get_last_selected() or {}
            from vtx.agent import Agent

            agent = Agent(name="quiz-judge",
                          model=sel.get("model_id") or "gpt-4o-mini",
                          instructions="You are a strict quiz grader. Output only JSON.")
            stream = Runner.run_streamed(agent, prompt, session=InMemorySession())
            text = ""
            async for ev in stream:
                d = getattr(ev, "delta", None)
                if d:
                    text += d
            result = getattr(stream, "result", None)
            final = getattr(result, "final_output", None) or text
            try:
                return json.loads(str(final).strip().strip("`").removeprefix("json").strip())
            except Exception:  # noqa: BLE001
                return {"score": None, "feedback": str(final)[:500]}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"judge failed: {exc}"}

    async def _quiz_judge(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Grade a quiz answer with a real LLM call through the vtx runtime."""
        question = str(params.get("question") or "")
        answer = str(params.get("answer") or params.get("submission") or "")
        reference = str(params.get("reference") or params.get("expected") or "")
        prompt = (
            "Grade this quiz answer. Reply with JSON: "
            '{"score":0-100,"correct":bool,"feedback":"..."}\n\n'
            f"Question: {question}\nAnswer: {answer}\n"
            + (f"Reference: {reference}\n" if reference else "")
        )
        result_text = ""
        try:
            from vtx.agent import Agent
            from vtx.agent.sdk.runner import Runner
            from vtx.agent.sdk.sessions import InMemorySession
            from vtx.core.permissions import AutoApprove

            agent = self._runtime_agent() if hasattr(self, "_runtime_agent") else None
            if agent is None:
                # Minimal grading agent on the configured default model.
                from vtx.ai.models import get_model
                from vtx.core.config import get_last_selected

                sel = get_last_selected() or {}
                agent = Agent(name="quiz-judge", model=sel.get("model_id") or "gpt-4o-mini",
                              instructions="You are a strict quiz grader. Output only JSON.")
            stream = Runner.run_streamed(agent, prompt, session=InMemorySession())
            async for ev in stream:
                d = getattr(ev, "delta", None)
                if d:
                    result_text += d
            result = getattr(stream, "result", None)
            final = getattr(result, "final_output", None) or result_text
            try:
                grade = json.loads(str(final).strip().strip("`").removeprefix("json").strip())
            except Exception:  # noqa: BLE001
                grade = {"score": None, "feedback": str(final)[:500]}
            return {"jsonrpc": "2.0", "id": req_id, "result": grade}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": JSON_RPC_INTERNAL_ERROR, "message": f"judge failed: {exc}"}}

    async def _book_stream(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Stream book content as session/update-style notifications."""
        book_id = str(params.get("book_id") or params.get("id") or "")
        path = self.app.state.settings.data_dir / "books" / f"{book_id}.json"
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            data = {"id": book_id, "title": book_id, "chunks": []}
        chunks = data.get("chunks") or [data.get("content", "")]
        session_id = str(params.get("sessionId") or "book")
        for i, chunk in enumerate(chunks):
            await self._update(session_id, {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": str(chunk)},
                "_meta": {"book_chunk": i, "book_id": book_id},
            })
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"book_id": book_id, "chunks": len(chunks)}}

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()


