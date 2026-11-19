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
from vtx.core.events import AskUserEvent
from vtx.core.permissions import AskUserAnswer, AskUserResponse
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
  - `paper_search` — arXiv papers for research questions
  - `video` — for videos/animations: call it with a plain-language `query`
    (e.g. "explain photosynthesis in 90 seconds"); a video sub-agent writes
    and renders the film. Never write Remotion code yourself.
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
    # Stable id used by the frontend to group one model call's trace rows.
    call_id: str = ""
    # Live session dict the turn is writing to. Events are recorded into it
    # as they are emitted so a client that misses live frames (short turn,
    # late pump subscription, reconnect) can replay them from the store.
    session_ref: dict[str, Any] | None = None

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
    """Owns sessions, turns, and the vtx agent loop for the REST/SSE endpoint."""

    def __init__(self, settings: Settings, store: SessionStore) -> None:
        self._settings = settings
        self._store = store
        self._turns: dict[str, TurnHandle] = {}
        self._vtx_runtimes: dict[str, Any] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # turn_id -> (future to resolve, tool_call_id, question prompts by id)
        self._ask_user_waiters: dict[str, tuple[Any, str, dict[str, str]]] = {}

    # -- turn registry ----------------------------------------------------

    def active_turn_for_session(self, session_id: str) -> TurnHandle | None:
        for turn in self._turns.values():
            if turn.session_id == session_id and turn.status == "running":
                return turn
        return None

    def get_turn(self, turn_id: str) -> TurnHandle | None:
        return self._turns.get(turn_id)

    async def resolve_ask_user(
        self,
        turn_id: str,
        text: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        """Feed a learner's answer into a paused ask_user tool call.

        Resolves the SDK's future so the same turn resumes with the answer
        substituted into the tool result. Emits the resolution event the UI
        uses to seal the question card. Returns False when no question is
        currently waiting on this turn (e.g. after a backend restart).
        """
        waiter = self._ask_user_waiters.pop(turn_id, None)
        if waiter is None:
            return False
        future, tool_call_id, prompts = waiter
        resolved: list[dict[str, Any]] = []
        sdk_answers: list[AskUserAnswer] = []
        for a in answers:
            if not isinstance(a, dict):
                continue
            qid = str(a.get("questionId") or a.get("id") or "").strip()
            value = str(a.get("text") or "").strip()
            question = prompts.get(qid, qid)
            resolved.append({"questionId": qid, "text": value})
            if value:
                sdk_answers.append(
                    AskUserAnswer(question=question, kind="custom", answer=value)
                )
        if text.strip() and not sdk_answers:
            # Legacy flat reply: attribute it to the first pending question.
            first = next(iter(prompts.values()), "")
            sdk_answers.append(
                AskUserAnswer(question=first, kind="custom", answer=text.strip())
            )
        response = AskUserResponse(
            custom_text=text.strip() or None,
            answers=tuple(sdk_answers),
        )
        if future is not None and not future.done():
            future.set_result(response)
        handle = self._turns.get(turn_id)
        if handle is not None:
            await self._emit(handle, self._stream_event(
                handle, "progress", source="ask_user", stage="responding",
                metadata={
                    "ask_user_resolved": True,
                    "ask_user_tool_call_id": tool_call_id,
                    "answers": resolved,
                    "reply_preview": text.strip(),
                }))
        return True

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

    @staticmethod
    def _context_budget(runtime: Any, session: dict[str, Any], usage: Any) -> dict[str, Any]:
        """Build the context-budget payload for the composer's context bar."""
        def tokens(value: Any) -> int:
            if isinstance(value, list):
                value = "".join(
                    str(b.get("text") or "") if isinstance(b, dict) else str(b)
                    for b in value
                )
            return max(0, len(str(value or "")) // 4)

        segments: list[dict[str, Any]] = []
        segments.append({
            "key": "messages",
            "tokens": sum(tokens(m.get("content")) for m in session.get("messages") or []),
        })
        prompt = str(getattr(getattr(runtime, "agent", None), "_system_prompt", "") or "")
        segments.append({"key": "system_prompt", "tokens": tokens(prompt)})
        segments.append({
            "key": "system_tools",
            "tokens": 120 * len(getattr(runtime, "tools", None) or []),
        })
        estimated_used = sum(seg["tokens"] for seg in segments)

        window = 0
        try:
            from vtx.ai import get_model

            info = get_model(getattr(runtime, "model", ""), getattr(runtime, "model_provider", ""))
            window = int(getattr(info, "context_window", 0) or 0)
        except Exception:  # noqa: BLE001
            pass
        window_estimated = window <= 0
        if window_estimated:
            window = 200_000

        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
        used = input_tokens if input_tokens > 0 else estimated_used
        return {
            "window": window,
            "window_estimated": window_estimated,
            "used_tokens": used,
            "free_tokens": max(0, window - used),
            "model": f"{getattr(runtime, 'model_provider', '')}/{getattr(runtime, 'model', '')}".strip("/"),
            "counter": "provider_usage" if input_tokens > 0 else "heuristic",
            "segments": segments,
        }

    async def _emit(self, turn: TurnHandle, event: dict[str, Any]) -> None:
        turn.events.append(event)
        # Mirror into the session record so turn_events_after() can replay the
        # suffix once the in-memory handle is dropped on completion. The dict
        # is persisted once the turn finalizes (not on every token).
        if turn.session_ref is not None:
            self._store.record_turn_event(turn.session_ref, turn.turn_id, event)
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
        handle = TurnHandle(
            turn_id=turn_id,
            session_id=session["id"],
            session_ref=session,
            call_id=f"call_{uuid.uuid4().hex[:12]}",
        )
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
                ToolApprovalEvent, ToolEndEvent, ToolResultEvent, ToolStartEvent,
            )
            from vtx.core.permissions import ApprovalResponse
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
                # Mirror the vtx coding agent's _ensure_agent: agent registry,
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
                # Bind this DeepTutor record while vtx builds its Session, so
                # the session's entries are loaded from / saved to our JSON
                # document instead of ~/.vtx/sessions/*.jsonl.
                from .dt_session import bind as bind_dt_session, unbind as unbind_dt_session

                dt_token = bind_dt_session(self._store, session)
                try:
                    init = await asyncio.to_thread(runtime.initialize)
                finally:
                    unbind_dt_session(dt_token)
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
                    metadata={"status": "error",
                              "user_message_id": user_message.get("id"),
                              "assistant_message_id": None}))
                self._finalize_turn(handle, session, "error")
                return True
        else:
            runtime, agent = self._vtx_runtimes[cache_key]
        await self._emit(handle, self._stream_event(
            handle, "stage_start", source=capability, stage="responding"))

        status = "completed"
        assistant_parts: list[str] = []
        usage: Any = None
        try:
            async for event in agent.run(
                prompt,
                cancel_event=handle.cancel,
            ):
                if isinstance(event, AskUserEvent):
                    # The SDK intercepts ask_user and hands us the questionnaire
                    # plus a future for the learner's answer. Publish a card to
                    # the client now and let submit_user_reply resolve the future.
                    prompts: dict[str, str] = {}
                    questions = []
                    for i, q in enumerate(event.questions or []):
                        qid = f"q{i + 1}"
                        prompts[qid] = q.question
                        questions.append({
                            "id": qid,
                            "prompt": q.question,
                            "header": q.header or None,
                            "multi_select": bool(q.multi_select),
                            "options": [
                                {"label": o.label, "description": o.description or None}
                                for o in (q.options or [])
                            ],
                            "allow_free_text": True,
                            "placeholder": None,
                        })
                    await self._emit(handle, self._stream_event(
                        handle, "tool_result", source=capability,
                        stage="responding",
                        metadata={
                            "tool_call_id": event.tool_call_id,
                            "tool_name": "ask_user",
                            "tool_metadata": {"ask_user": {"questions": questions}},
                        }))
                    self._ask_user_waiters[handle.turn_id] = (
                        event.future, event.tool_call_id, prompts)
                    continue
                if isinstance(event, ThinkingDeltaEvent):
                    if event.delta:
                        # The frontend groups trace rows by metadata.call_id.
                        # Without it, reasoning deltas are dropped by
                        # groupTraceEvents() and never render at all. A single
                        # id per turn groups one model call's reasoning into
                        # one expandable row, and llm_reasoning keeps the
                        # activity header open while it is streaming.
                        await self._emit(handle, self._stream_event(
                            handle, "thinking", content=event.delta,
                            source=capability, stage="responding",
                            metadata={
                                "call_id": handle.call_id,
                                "call_kind": "llm_reasoning",
                                "trace_kind": "llm_reasoning",
                                "call_role": "assistant",
                            }))
                elif isinstance(event, TextDeltaEvent):
                    if event.delta:
                        assistant_parts.append(event.delta)
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
                elif isinstance(event, ToolApprovalEvent):
                    # DeepTutor has no permission UI — auto-approve tool calls.
                    if event.future is not None and not event.future.done():
                        event.future.set_result(ApprovalResponse.APPROVE)
                elif isinstance(event, ToolResultEvent):
                    out = event.result
                    text = getattr(out, "output", None) or (
                        out if isinstance(out, str) else
                        __import__("json").dumps(out, ensure_ascii=False, default=str))
                    text = str(text)
                    tool_meta: dict[str, Any] = {}
                    # The video tool writes an mp4 into the /files tree and markers
                    # its result with a machine-readable prefix. Turn that into an
                    # artifact so the chat renders an inline <video> player.
                    if (
                        event.tool_name == "video"
                        and text.startswith("VIDEO_ARTIFACT:")
                        and not getattr(out, "is_error", False)
                    ):
                        marker, _, readable = text.partition("|")
                        rel = marker.split(":", 1)[1].strip()
                        if rel and ".." not in rel and not rel.startswith("/"):
                            tool_meta["artifacts"] = [{
                                "filename": rel.rsplit("/", 1)[-1],
                                "url": f"/files/{rel}",
                                "mime_type": "video/mp4",
                                "generated": True,
                            }]
                            text = readable or "Video ready."
                    await self._emit(handle, self._stream_event(
                        handle, "tool_result", content=text,
                        source=capability, stage="responding",
                        metadata={"tool_call_id": event.tool_call_id,
                                  "tool_name": event.tool_name,
                                  "is_error": bool(getattr(out, "is_error", False)),
                                  "tool_metadata": tool_meta}))
                elif isinstance(event, ErrorEvent):
                    status = "error"
                    # Surface the provider/agent failure to the UI. Without an
                    # explicit frame the turn just ends with status=error and
                    # the user sees an empty reply with no explanation.
                    await self._emit(handle, self._stream_event(
                        handle, "error",
                        content=str(getattr(event, "error", "") or "Agent failed."),
                        source=capability, stage="responding",
                        metadata={"turn_terminal": True}))
                elif isinstance(event, AgentEndEvent):
                    usage = getattr(event, "total_usage", None)
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001
            status = "error"
            await self._emit(handle, self._stream_event(
                handle, "error", content=str(exc), source=capability,
                stage="responding"))
        # Emit the context-window readout the composer's context bar consumes.
        # The UI reads metadata.metadata.context_budget from a `result` event;
        # without it the bar never renders, even though vtx measures usage.
        await self._emit(handle, self._stream_event(
            handle, "result", source=capability, stage="responding",
            metadata={"metadata": {"context_budget": self._context_budget(
                runtime, session, usage)}}))
        assistant_text = "".join(assistant_parts).strip()
        assistant_message: dict[str, Any] | None = None
        if assistant_text:
            try:
                assistant_message = self._store.append_assistant_message(
                    session, assistant_text,
                    events=list(handle.events), turn_id=handle.turn_id,
                    capability=capability,
                    metadata={"status": status},
                )
            except Exception:  # noqa: BLE001
                pass
        session["status"] = "completed" if status == "completed" else status
        session["last_output"] = assistant_text[:500]
        try:
            self._store.save(session)
        except Exception:  # noqa: BLE001
            pass
        handle.status = status

        await self._emit(handle, self._stream_event(
            handle, "stage_end", source=capability, stage="responding",
            metadata={"status": status}))
        # The client reconciles its optimistic rows and clears failed-
        # submission records from these ids. Without them every completed
        # turn was treated as an unsent draft despite being persisted.
        done_meta: dict[str, Any] = {
            "status": status,
            "user_message_id": user_message.get("id"),
            "assistant_message_id": (
                assistant_message.get("id") if assistant_message else None
            ),
        }
        await self._emit(handle, self._stream_event(
            handle, "done", source=capability, stage="responding",
            metadata=done_meta))
        # Persist the recorded turn (incl. done) before the done callback drops
        # the in-memory handle — replay must find these frames in the store.
        self._finalize_turn(handle, session, status)
        return True

    def _finalize_turn(
        self, handle: TurnHandle, session: dict[str, Any], status: str
    ) -> None:
        """Seal the recorded turn and flush it to disk."""
        turn = session.get("turns", {}).get(handle.turn_id)
        if turn is not None:
            turn["status"] = status
            turn["finished_at"] = time.time()
            turn["last_seq"] = handle.seq
        handle.status = status
        try:
            self._store.save(session)
        except Exception:  # noqa: BLE001
            pass

    async def _run(
        self,
        handle: TurnHandle,
        session: dict[str, Any],
        command: dict[str, Any],
        user_message: dict[str, Any],
    ) -> None:
        capability = str(command.get("capability") or "chat").strip() or "chat"
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
