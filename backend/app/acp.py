"""ACP agent core — transport-agnostic JSON-RPC 2.0, mirroring vtx's
``coding_agent/acp.py`` structure: ``_send`` / ``_respond`` / ``_error`` /
``_notify`` / ``_dispatch`` over plain dicts, sessions own their runtime
state, agent->client traffic is ``session/update`` notifications.

No web-framework types here — transports (HTTP POST, WebSocket) adapt
messages to :meth:`AcpAgent.handle` and drain :meth:`AcpAgent.send`.
"""

from __future__ import annotations

import asyncio
import base64
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
    "http.request",
    "system.list_methods",
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
            self._spawn_legacy(msg)
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

        # turn.* JSON-RPC aliases -> legacy command handler
        ctype = TURN_METHODS.get(method)
        if ctype is not None:
            self._spawn_legacy({"type": ctype, **params})
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

    def _spawn_legacy(self, command: dict[str, Any]) -> None:
        from .api.ws import Connection  # local import: ws imports runtime only

        if not hasattr(self, "_legacy"):
            # The legacy Connection needs a websocket-like object; transports
            # that support it inject one via attach_legacy(). Without it we
            # fall back to driving the runtime directly for start_turn.
            self._drive_legacy(command)
            return
        self._legacy.spawn(self._legacy.handle(command))

    def attach_legacy(self, websocket: Any) -> None:
        """Bind a legacy turn-protocol Connection (WS transport only)."""
        from .api.ws import Connection

        self._legacy = Connection(websocket, self.runtime)

        async def _wrapped_send(event: dict[str, Any]) -> None:
            await self._notify("turn.event", event)

        self._legacy.send = _wrapped_send  # type: ignore[method-assign]

    async def _drive_legacy(self, command: dict[str, Any]) -> None:
        """Handle a turn command without a websocket (HTTP-only transports).

        Only start_turn is meaningful here; subscribe/cancel need WS.
        """
        ctype = command.get("type")
        if ctype in ("start_turn", "message"):
            handle, session_event, error = await self.runtime.start_turn(command)
            if error or handle is None:
                await self._error(
                    None, JSON_RPC_INVALID_PARAMS, error or "failed to start turn"
                )
                return
            if session_event is not None:
                await self.runtime._emit(handle, session_event)
                await self._notify("turn.event", session_event)
            await self._pump_raw(handle)
        elif ctype == "cancel_turn":
            handle = self.runtime.get_turn(str(command.get("turn_id") or ""))
            if handle is not None and handle.status == "running":
                handle.cancel.set()

    async def _pump_raw(self, handle: Any) -> None:
        """Stream a turn's events as turn.event notifications until done."""
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
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"sessions": [_session_summary(s) for s in sessions]}}

        if method == "session/load":
            session_id = str(p.get("sessionId") or p.get("session_id") or "")
            session = self.store.get(session_id)
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

        if method == "http.request":
            return await self._http_request(req_id, p)

        if method == "system.list_methods":
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"methods": sorted(ACP_METHODS)}}

        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": JSON_RPC_METHOD_NOT_FOUND,
                          "message": f"Unknown method: {method}"}}

    async def _http_request(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Forward a REST-shaped call through the app's ASGI stack."""
        import httpx

        method = str(params.get("method") or "GET").upper()
        path = str(params.get("path") or "/api/health")
        query = params.get("query") or {}
        body = params.get("body")
        headers = {"content-type": "application/json",
                   **(params.get("headers") or {})}

        kwargs: dict[str, Any] = {}
        if isinstance(query, dict) and query:
            kwargs["params"] = query
        if body is not None:
            if isinstance(body, dict) and "__multipart__" in body:
                kwargs["files"] = _multipart_files(body["__multipart__"])
                kwargs["data"] = _multipart_fields(body["__multipart__"])
            else:
                kwargs["content"] = json.dumps(body)

        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://rpc.internal"
        ) as client:
            try:
                resp = await client.request(method, path, headers=headers, **kwargs)
            except Exception as exc:  # noqa: BLE001
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": JSON_RPC_INTERNAL_ERROR,
                                  "message": f"Dispatch failed: {exc}"}}
        try:
            payload: Any = resp.json()
        except Exception:  # noqa: BLE001
            payload = resp.text
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"status": resp.status_code, "body": payload}}

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if hasattr(self, "_legacy"):
            self._legacy.unsubscribe()


def _multipart_fields(spec: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in (spec.get("fields") or {}).items()}


def _multipart_files(spec: dict[str, Any]) -> dict[str, tuple[str, bytes, str]]:
    files: dict[str, tuple[str, bytes, str]] = {}
    for f in spec.get("files") or []:
        name = str(f.get("name") or "file")
        filename = str(f.get("filename") or name)
        ctype = str(f.get("content_type") or "application/octet-stream")
        files[name] = (filename, base64.b64decode(str(f.get("data_b64") or "")), ctype)
    return files
