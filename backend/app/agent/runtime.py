"""Turn runtime: builds vtx Agents and maps the SDK event stream onto the
DeepTutor web turn protocol (``web/contracts/generated/turn-protocol.ts``).

Event mapping (vtx SDK → wire):

======================================  ======================================
vtx event                                wire StreamEvent
======================================  ======================================
``_AgentStartEvent``                     ``stage_start`` (once per turn)
``_TextDelta``                           ``content``
``_ToolCallEnd`` (arguments known)       ``tool_call``
``_ToolResult``                          ``tool_result``
run finished                             ``result`` then ``done``
exception / guardrail trip               ``error`` then ``done`` (failed)
cancellation set                         ``done`` (cancelled)
======================================  ======================================

Known SDK gaps we design around:

  ``thinking`` wire event is therefore unused until vtx exposes them.
* Approvals pause a run via ``RunState`` but cannot be resumed through
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from vtx.agent.tools.base import BaseTool
from vtx.protocol.types import ToolResult

from ..config import Settings
from ..store.session_store import SessionStore
from .tools import builtin_tools

log = logging.getLogger("deeptutor.runtime")

PROTOCOL_VERSION = "2.0"

# underscore-prefixed and not exported, so we match on type name — the same
# dispatch the SDK docs recommend.
def _event_name(event: Any) -> str:
    return type(event).__name__


TUTOR_INSTRUCTIONS = """\
You are DeepTutor, a patient and rigorous personal tutor.

- Explain concepts clearly, step by step, at the learner's level.
- Use the tools that fit the task:
  - `web` — current information, news, fetching pages (mode='search'|'fetch')
  - `knowledge_base_search` — the learner's uploaded documents; always
    search before answering questions about their materials
  - `memory` — remember durable facts about the learner across sessions
  - `notes` — save study notes, summaries, and reference material
  - `learning`/`mastery` — track progress and run guided-learning sessions
  - `workspace`/`apply_patch`/`read_file`/`list_files` — files and artifacts
  - `quiz` — generate or grade quiz questions
  - `paper_search` — arXiv papers for research questions
  - `video` — render Remotion videos when asked for one
  - `codemode` — multi-step work: write Python that calls other tools;
    `%bash <cmd>` runs shell commands inside the script
  - `tool_search` — discover which tool fits when unsure
  - `ask_user` — ask the learner a clarifying question and wait for the reply
- For multi-step tasks prefer `codemode` over many separate tool calls.
- Ask a clarifying question only when the request is genuinely ambiguous.
- Keep answers focused; use Markdown for structure.
"""


@dataclass
class TurnHandle:
    turn_id: str
    session_id: str
    status: str = "running"
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq



class _SdkToolAdapter(BaseTool):
    """Adapt a vtx SDK ``@tool`` FunctionTool to the ``BaseTool`` interface
    ConversationRuntime expects."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn
        self.name = getattr(fn, "name", "tool")
        self.description = getattr(fn, "description", "") or ""
        self.tool_icon = getattr(fn, "tool_icon", "🛠️")
        params = getattr(fn, "params", None)
        self.params = params if isinstance(params, type) else dict
        self.mutating = bool(getattr(fn, "mutating", True))

    def format_call(self, params: Any) -> str:
        return self.name

    def format_preview(self, params: Any) -> str | None:
        return None

    async def execute(self, params: Any, cancel_event: Any = None) -> ToolResult:
        try:
            res = await self._fn.execute(params)
            text = getattr(res, "result", None)
            if text is None:
                text = str(res)
            return ToolResult(success=True, result=str(text))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, result=f"{self.name} error: {exc}")



class AgentRuntime:
    """Owns sessions, turns, and the vtx agent loop for the WS endpoint."""

    def __init__(self, settings: Settings, store: SessionStore) -> None:
        self._settings = settings
        self._store = store
        self._turns: dict[str, TurnHandle] = {}
        self._vtx_runtimes: dict[str, Any] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    # -- turn registry ----------------------------------------------------

    def active_turn_for_session(self, session_id: str) -> TurnHandle | None:
        for turn in self._turns.values():
            if turn.session_id == session_id and turn.status == "running":
                return turn
        return None

    def get_turn(self, turn_id: str) -> TurnHandle | None:
        return self._turns.get(turn_id)

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    # -- agent construction -------------------------------------------------

    @staticmethod
    def _vtx_llm_defaults() -> tuple[str | None, str | None]:
        """(provider_slug, model_id) from the user's ``~/.vtx/config.yml``."""
        try:
            from vtx.core.config import get_config

            cfg = get_config()
            return cfg.llm.default_provider, cfg.llm.default_model
        except Exception:  # noqa: BLE001 - config is best-effort
            return None, None

    @staticmethod
    def _saved_selection() -> tuple[str | None, str | None]:
        """Provider/model chosen in the web settings panel, if any."""
        try:
            import json

            from ..config import get_settings

            path = get_settings().data_dir / "settings.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            sel = data.get("llm_selection") or {}
            return sel.get("profile_id"), sel.get("model_id")
        except Exception:
            return None, None

    @staticmethod
    def _is_builtin_provider(name: str) -> bool:
        try:
            from vtx.ai.dynamic_models import DYNAMIC_PROVIDERS
            from vtx.ai.provider_catalog import list_providers

            return name in {p.slug for p in list_providers()} or name in DYNAMIC_PROVIDERS
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _stream_event(
        turn: TurnHandle,
        etype: str,
        *,
        content: str = "",
        source: str = "",
        stage: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": etype,
            "protocol_version": PROTOCOL_VERSION,
            "seq": turn.next_seq(),
            "session_id": turn.session_id,
            "turn_id": turn.turn_id,
            "timestamp": time.time(),
            "source": source,
            "stage": stage,
            "content": content,
            "metadata": metadata or {},
        }

    async def _emit(self, turn: TurnHandle, event: dict[str, Any]) -> None:
        turn.events.append(event)
        dead: list[asyncio.Queue] = []
        for queue in turn.subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            turn.subscribers.discard(queue)

    # -- turn execution ------------------------------------------------------

    async def start_turn(
        self,
        command: dict[str, Any],
    ) -> tuple[TurnHandle | None, dict[str, Any] | None, str | None]:
        """Validate and launch a turn. Returns (handle, session_event, error).

        The returned ``session_event`` must be emitted first so the client
        learns the (possibly new) session_id before stream events arrive.
        """
        content = str(command.get("content") or "")
        attachments = command.get("attachments") or []
        if not content.strip() and not attachments:
            return None, None, "Turn content must not be empty"

        session_id = (command.get("session_id") or "").strip() or None
        session = self._store.get(session_id) if session_id else None
        created = False
        if session is None:
            session = self._store.create(
                preferences={"workspace_id": command.get("workspace_id")}
            )
            created = True

        capability = (command.get("capability") or "chat").strip() or "chat"
        turn_id = _new_id("turn")
        handle = TurnHandle(turn_id=turn_id, session_id=session["id"])
        self._turns[turn_id] = handle

        # Persist the user row up front so a crash never loses the prompt.
        user_message = self._store.append_user_message(
            session,
            content,
            attachments=attachments,
            metadata={"capability": capability, "turn_id": turn_id},
        )
        session["status"] = "running"
        session["last_input"] = content
        self._store.save(session)

        session_event = self._stream_event(
            handle,
            "session",
            metadata={"session_id": session["id"], "turn_id": turn_id},
        )

        task = asyncio.create_task(
            self._run(handle, session, command, user_message)
        )
        task.add_done_callback(lambda _t: self._turns.pop(turn_id, None))
        return handle, session_event, None

    async def _run_vtx(
        self,
        handle: TurnHandle,
        session: dict[str, Any],
        command: dict[str, Any],
        user_message: dict[str, Any],
    ) -> bool:
        """Drive a turn through vtx's ConversationRuntime (coding_agent path).

        Streams ThinkingDeltaEvent live — unlike the SDK Runner which buffers
        reasoning. Returns True if the turn completed through this path.
        """
        try:
            from vtx.agent.runtime import ConversationRuntime
            from vtx.core.config import config as vtx_config, get_last_selected
            from vtx.core.events import (
                AgentEndEvent, ErrorEvent, TextDeltaEvent, ThinkingDeltaEvent,
                ToolEndEvent, ToolResultEvent, ToolStartEvent,
            )
        except Exception:  # noqa: BLE001
            return False

        capability = (command.get("capability") or "chat").strip() or "chat"
        content = str(command.get("content") or "")
        attachments = command.get("attachments") or []
        prompt = content
        if attachments:
            notes = [f"[Attachment: {a.get('filename') or a.get('id') or 'attachment'}]"
                     + (f"\n{a['extracted_text']}" if a.get("extracted_text") else "")
                     for a in attachments]
            prompt = "\n\n".join(([content] if content else []) + notes)

        # Per-session runtime cache — ConversationRuntime owns the vtx session.
        cache_key = session["id"]
        runtime = self._vtx_runtimes.get(cache_key)
        if runtime is None:
            try:
                # Mirror coding_agent/acp.py _ensure_agent: agent registry,
                # plugin extensions, then ConversationRuntime + prepare_for_run.
                import os as _os
                from vtx.agent.agents import AgentRegistry, load_all_agents
                from vtx.plugins.extensions import load_for_runtime

                last = get_last_selected()
                cwd = str(self._settings.workspace_dir)
                provider = (command.get("model_provider") or last.provider
                            or vtx_config.llm.default_provider)
                model = (command.get("model") or session.get("model")
                         or last.model_id or vtx_config.llm.default_model)

                agent_registry = AgentRegistry()
                loaded_agents, agent_errors = load_all_agents(cwd=cwd)
                agent_registry.agents = loaded_agents
                agent_registry.errors = agent_errors
                desired = (_os.environ.get("VTX_AGENT")
                           or (last.agent or None)
                           or (vtx_config.agents.default or None))
                if desired:
                    agent_registry.set_active(desired)

                loaded_extensions = load_for_runtime(cwd=cwd)
                ext_tools = list(loaded_extensions.list_extension_tools())
                if agent_registry.active is not None:
                    ext_tools.extend(agent_registry.active.local_tools.values())
                    ext_tools.extend(
                        loaded_extensions.local_tools_for(
                            agent_registry.active.definition.name))
                from .tools import builtin_tools as _bt
                fns = _bt(self._settings, command.get("tools"))
                tools = [_SdkToolAdapter(t) for t in fns] + ext_tools

                runtime = ConversationRuntime(
                    cwd=cwd, model=model, model_provider=provider,
                    base_url=vtx_config.llm.default_base_url or None,
                    thinking_level=(command.get("thinking_level")
                                    or last.thinking_level
                                    or vtx_config.llm.default_thinking_level),
                    tools=tools,
                    extensions=loaded_extensions.bus,
                    agent_registry=agent_registry,
                    active_agent=agent_registry.active,
                    agent_extensions=list(loaded_extensions.extensions),
                )
                runtime.set_loaded_extensions(loaded_extensions)
                # Resume the vtx-native session so DeepTutor chats continue
                # the same JSONL history the vtx CLI/TUI sees.
                resume_id = None
                vtx_file = session.get("vtx_session_file")
                if vtx_file:
                    try:
                        from vtx.agent.session import Session as _VS
                        resume_id = _VS.load(vtx_file).id
                    except Exception:  # noqa: BLE001
                        resume_id = None
                init = await asyncio.to_thread(
                    runtime.initialize, resume_session=resume_id)
                if getattr(init, "provider_error", None):
                    raise RuntimeError(str(init.provider_error))
                agent = await asyncio.to_thread(runtime.prepare_for_run)
                if agent is None:
                    raise RuntimeError("agent initialization failed")

                # MCP + hooks, mirroring coding_agent: failures warn, never
                # fail the run.
                try:
                    await runtime.connect_mcp()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from pathlib import Path
                    from vtx.agent.hooks.bridge import HookBridge
                    hb = HookBridge(
                        bus=loaded_extensions.bus,
                        project_path=Path(cwd) / ".vtx" / "hooks.yml",
                        global_path=Path.home() / ".vtx" / "hooks.yml",
                    )
                    await hb.load()
                except Exception:  # noqa: BLE001
                    pass
                self._vtx_runtimes[cache_key] = (runtime, agent)
            except Exception as exc:  # noqa: BLE001
                await self._emit(handle, self._stream_event(
                    handle, "error", content=f"agent init failed: {exc}",
                    source=capability, stage="responding"))
                await self._emit(handle, self._stream_event(
                    handle, "done", source=capability, stage="responding",
                    metadata={"status": "error"}))
                return True
        else:
            runtime, agent = self._vtx_runtimes[cache_key]

        await self._emit(handle, self._stream_event(
            handle, "stage_start", source=capability, stage="responding"))

        status = "completed"
        try:
            async for event in agent.run(
                prompt,
                cancel_event=handle.cancel,
            ):
                if isinstance(event, ThinkingDeltaEvent):
                    if event.delta:
                        await self._emit(handle, self._stream_event(
                            handle, "thinking", content=event.delta,
                            source=capability, stage="responding"))
                elif isinstance(event, TextDeltaEvent):
                    if event.delta:
                        await self._emit(handle, self._stream_event(
                            handle, "content", content=event.delta,
                            source=capability, stage="responding"))
                elif isinstance(event, ToolStartEvent):
                    await self._emit(handle, self._stream_event(
                        handle, "progress",
                        content=f"Calling {event.tool_name}",
                        source=capability, stage="responding",
                        metadata={"tool_name": event.tool_name,
                                  "tool_call_id": event.tool_call_id}))
                elif isinstance(event, ToolEndEvent):
                    await self._emit(handle, self._stream_event(
                        handle, "tool_call", content=event.tool_name,
                        source=capability, stage="responding",
                        metadata={"tool": event.tool_name,
                                  "tool_name": event.tool_name,
                                  "tool_call_id": event.tool_call_id,
                                  "args": getattr(event, "arguments", None)}))
                elif isinstance(event, ToolResultEvent):
                    out = event.result
                    text = getattr(out, "output", None) or (
                        out if isinstance(out, str) else
                        __import__("json").dumps(out, ensure_ascii=False, default=str))
                    await self._emit(handle, self._stream_event(
                        handle, "tool_result", content=str(text),
                        source=capability, stage="responding",
                        metadata={"tool_call_id": event.tool_call_id,
                                  "tool_name": event.tool_name,
                                  "is_error": bool(getattr(out, "is_error", False))}))
                elif isinstance(event, ErrorEvent):
                    status = "error"
                elif isinstance(event, AgentEndEvent):
                    pass
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001
            status = "error"
            await self._emit(handle, self._stream_event(
                handle, "error", content=str(exc), source=capability,
                stage="responding"))

        await self._emit(handle, self._stream_event(
            handle, "stage_end", source=capability, stage="responding",
            metadata={"status": status}))
        await self._emit(handle, self._stream_event(
            handle, "done", source=capability, stage="responding",
            metadata={"status": status}))
        return True

    async def _run(
        self,
        handle: TurnHandle,
        session: dict[str, Any],
        command: dict[str, Any],
        user_message: dict[str, Any],
    ) -> None:
        await self._run_vtx(handle, session, command, user_message)
        return
    def turn_events_after(self, turn_id: str, after_seq: int) -> list[dict[str, Any]]:
        handle = self._turns.get(turn_id)
        if handle is not None:
            return [e for e in handle.events if (e.get("seq") or 0) > after_seq]
        # Fall back to the persisted log (turn finished or server restarted).
        for path in self._store._settings.sessions_dir.glob("*.json"):
            session = self._store.get(path.stem)
            if not session:
                continue
            turn = session.get("turns", {}).get(turn_id)
            if turn:
                return [
                    e for e in turn.get("events", []) if (e.get("seq") or 0) > after_seq
                ]
        return []

    def latest_turn_for_session(self, session_id: str) -> str | None:
        session = self._store.get(session_id)
        if not session:
            return None
        turns = session.get("turns", {})
        if not turns:
            return None
        return max(turns, key=lambda t: turns[t].get("created_at", 0))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
