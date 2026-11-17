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

* Thinking deltas are consumed inside ``Runner`` but never yielded — the
  ``thinking`` wire event is therefore unused until vtx exposes them.
* Approvals pause a run via ``RunState`` but cannot be resumed through
  ``Runner.run*``; we run with ``AutoApprove`` so tools never pause.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from vtx.agent.sdk import Agent, RunConfig, Runner
from vtx.agent.sdk.permissions import AutoApprove

from ..config import Settings
from ..store.session_store import SessionStore, vtx_jsonl_session
from .guardrails import block_sensitive_tool_args, limit_input_size
from .tools import builtin_tools

log = logging.getLogger("deeptutor.runtime")

PROTOCOL_VERSION = "2.0"

# Private event classes yielded by Runner.run_streamed. They are
# underscore-prefixed and not exported, so we match on type name — the same
# dispatch the SDK docs recommend.
def _event_name(event: Any) -> str:
    return type(event).__name__


TUTOR_INSTRUCTIONS = """\
You are DeepTutor, a patient and rigorous personal tutor.

- Explain concepts clearly, step by step, at the learner's level.
- Use the available tools when they help: fetch_url for current information,
  calculate for math, and the workspace file tools for notes and artifacts.
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


class AgentRuntime:
    """Owns sessions, turns, and the vtx agent loop for the WS endpoint."""

    def __init__(self, settings: Settings, store: SessionStore) -> None:
        self._settings = settings
        self._store = store
        self._turns: dict[str, TurnHandle] = {}
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

    def _build_agent(self, command: dict[str, Any]) -> Agent:
        llm = command.get("llm_selection") or {}
        saved_provider, saved_model = self._saved_selection()
        vtx_provider, vtx_model = self._vtx_llm_defaults()
        model = (
            (llm.get("model_id") or "").strip()
            or saved_model
            or self._settings.default_model
            or vtx_model
            or None
        )
        profile = (
            (llm.get("profile_id") or "").strip()
            or saved_provider
            or vtx_provider
            or ""
        )

        creds = self._catalog_credentials(profile) if profile else {}
        provider: Any = None
        if profile and self._is_builtin_provider(profile):
            # A built-in slug resolves through vtx's provider catalog; the
            # dict form lets us pin the model without a live provider object.
            # api_key/base_url from the web catalog act as overrides —
            # otherwise vtx resolves keys itself (env → ~/.vtx/dynamic_auth.json).
            provider = {"name": profile, "model": model}
            if creds.get("api_key"):
                provider["api_key"] = creds["api_key"]
            if creds.get("base_url"):
                provider["base_url"] = creds["base_url"]
        elif profile and creds.get("base_url"):
            # Custom OpenAI-compatible endpoint saved via the web UI: vtx
            # needs sdk + base_url for non-catalog providers.
            sdk = "anthropic" if creds.get("api_format") == "anthropic" else "openai"
            provider = {
                "name": profile,
                "sdk": sdk,
                "base_url": creds["base_url"],
                "model": model,
            }
            if creds.get("api_key"):
                provider["api_key"] = creds["api_key"]
        elif profile:
            log.warning("llm profile %r is not a vtx built-in provider; ignoring", profile)

        # Persist an explicit per-turn selection into vtx's config the same
        # way the TUI /model picker does — last_selected + recent_models in
        # ~/.vtx/config.yml. This is what survives a page refresh: the next
        # llm-options `active` reads it back. Only fires when the client
        # actually sent a selection (not the default fallback).
        if llm.get("profile_id") and llm.get("model_id"):
            try:
                from vtx.core.config import add_recent_model, set_last_selected

                set_last_selected(
                    model_id=llm["model_id"],
                    provider=llm["profile_id"],
                    thinking_level=llm.get("reasoning_effort"),
                )
                add_recent_model(llm["profile_id"], llm["model_id"])
            except Exception:  # noqa: BLE001 - persistence is best-effort
                pass

        tools_filter = command.get("tools")
        tools = builtin_tools(self._settings, tools_filter or None)

        persona = (command.get("persona") or "").strip()
        instructions = TUTOR_INSTRUCTIONS
        if persona:
            instructions += f"\n\nPersona: adopt the '{persona}' teaching persona."

        language = (command.get("language") or "").strip()
        if language:
            instructions += f"\n\nReply in the language identified by: {language}."

        agent = Agent(
            name="deeptutor",
            instructions=instructions,
            model=model,
            provider=provider,
            tools=tools,
            input_guardrails=[limit_input_size],
            # Permissions: never pause for approval inside a streamed turn —
            # the web protocol has no resume path for RunState yet.
        )
        # Eagerly resolve so we can repair local http:// endpoints: vtx's
        # OpenAISDK wrapper unconditionally upgrades http→https, which breaks
        # local gateways like the anyrouter proxy (127.0.0.1:8099). Passing a
        # live BaseProvider back keeps Runner from re-resolving the dict.
        try:
            resolved = agent.resolve_provider()
            self._repair_local_http_provider(resolved)
            agent.provider = resolved
        except Exception:  # noqa: BLE001 - let Runner surface the real error
            log.debug("provider pre-resolution failed", exc_info=True)
        return agent

    def _catalog_credentials(self, slug: str) -> dict[str, Any]:
        """API key / base_url a web-settings connection stored for ``slug``.

        Connections live in ``data/settings-catalog.json``; a connection's
        ``provider`` field is the vtx slug. Returns {} when nothing matches.
        """
        try:
            import json
            from pathlib import Path

            path = Path(self._settings.data_dir) / "settings-catalog.json"
            if not path.exists():
                return {}
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        for conn in catalog.get("connections", []) or []:
            if conn.get("provider") == slug:
                return {
                    "api_key": conn.get("api_key") or "",
                    "base_url": conn.get("base_url") or "",
                    "api_format": conn.get("api_format") or "auto",
                }
        # Fall back to llm service profiles bound to this provider slug.
        for profile in (catalog.get("services", {}).get("llm", {}) or {}).get("profiles", []) or []:
            if profile.get("provider") == slug:
                return {
                    "api_key": profile.get("api_key") or "",
                    "base_url": profile.get("base_url") or "",
                    "api_format": profile.get("api_format") or "auto",
                }
        return {}

    @staticmethod
    def _repair_local_http_provider(provider: Any) -> None:
        """Restore http:// base URLs that vtx's SDK layer upgrades to https.

        Only touches providers whose configured base_url is plaintext http —
        i.e. loopback/LAN gateways where TLS termination does not exist.
        """
        config = getattr(provider, "config", None)
        intended = getattr(config, "base_url", None) if config else None
        sdk = getattr(provider, "_sdk", None)
        if (
            sdk is not None
            and isinstance(intended, str)
            and intended.startswith("http://")
            and getattr(sdk, "base_url", "") != intended
        ):
            sdk.base_url = intended
            # Drop any cached client built with the https URL.
            if hasattr(sdk, "_async_client"):
                sdk._async_client = None
            log.info("repaired plaintext http base_url for provider %r", getattr(config, "provider", None))

    # -- event emission ------------------------------------------------------

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

    async def _run(
        self,
        handle: TurnHandle,
        session: dict[str, Any],
        command: dict[str, Any],
        user_message: dict[str, Any],
    ) -> None:
        capability = (command.get("capability") or "chat").strip() or "chat"
        turn_events: list[dict[str, Any]] = []
        assistant_text_parts: list[str] = []
        status = "completed"
        error_info: dict[str, Any] | None = None

        lock = self._lock_for(session["id"])
        async with lock:
            vtx_session = vtx_jsonl_session(self._store, session, str(self._settings.workspace_dir))
            agent = self._build_agent(command)
            config = RunConfig(permission_policy=AutoApprove())

            content = str(command.get("content") or "")
            attachments = command.get("attachments") or []
            prompt = content
            if attachments:
                notes = []
                for att in attachments:
                    name = att.get("filename") or att.get("id") or "attachment"
                    extracted = att.get("extracted_text")
                    notes.append(
                        f"[Attachment: {name}]"
                        + (f"\n{extracted}" if extracted else "")
                    )
                prompt = "\n\n".join([content, *notes]).strip() if content else "\n\n".join(notes)

            try:
                await self._emit(
                    handle,
                    self._stream_event(
                        handle,
                        "stage_start",
                        source=capability,
                        stage="responding",
                    ),
                )

                stream = Runner.run_streamed(
                    agent,
                    prompt,
                    session=vtx_session,
                    run_config=config,
                    max_turns=self._settings.max_turns,
                    cancellation=handle.cancel,
                )
                async for event in stream:
                    name = _event_name(event)
                    if name == "_TextDelta":
                        assistant_text_parts.append(event.delta)
                        await self._emit(
                            handle,
                            self._stream_event(
                                handle,
                                "content",
                                content=event.delta,
                                source=capability,
                                stage="responding",
                            ),
                        )
                    elif name == "_ToolCallEnd":
                        await self._emit(
                            handle,
                            self._stream_event(
                                handle,
                                "tool_call",
                                content=event.tool_name,
                                source=capability,
                                stage="responding",
                                metadata={
                                    "tool": event.tool_name,
                                    "tool_name": event.tool_name,
                                    "tool_call_id": event.tool_call_id,
                                    "args": event.arguments,
                                },
                            ),
                        )
                    elif name == "_ToolCallStart":
                        # Arguments only arrive on _ToolCallEnd; emit a
                        # lightweight progress marker so the UI shows activity.
                        await self._emit(
                            handle,
                            self._stream_event(
                                handle,
                                "progress",
                                content=f"Calling {event.tool_name}",
                                source=capability,
                                stage="responding",
                                metadata={
                                    "tool_name": event.tool_name,
                                    "tool_call_id": event.tool_call_id,
                                },
                            ),
                        )
                    elif name == "_ToolResult":
                        output = event.output
                        if not isinstance(output, str):
                            output = json.dumps(output, ensure_ascii=False, default=str)
                        await self._emit(
                            handle,
                            self._stream_event(
                                handle,
                                "tool_result",
                                content=output,
                                source=capability,
                                stage="responding",
                                metadata={
                                    "tool_call_id": event.tool_call_id,
                                    "tool_name": event.tool_name,
                                    "is_error": event.is_error,
                                },
                            ),
                        )

                result = stream.result
                # vtx buffers reasoning internally (ThinkingDeltaEvent is not
                # yielded on run_streamed); it lands on the result items as
                # ThinkingContent. Emit it as a `thinking` event so the UI
                # can show the model's reasoning after the fact.
                try:
                    from vtx.protocol.types import ThinkingContent

                    thoughts: list[str] = []
                    for item in getattr(result, "new_items", []) or []:
                        content = getattr(item, "content", None)
                        parts = content if isinstance(content, list) else [content]
                        for part in parts:
                            if isinstance(part, ThinkingContent) and part.thinking:
                                thoughts.append(part.thinking)
                    if thoughts:
                        await self._emit(
                            handle,
                            self._stream_event(
                                handle,
                                "thinking",
                                content="\n\n".join(thoughts),
                                source=capability,
                                stage="responding",
                            ),
                        )
                except Exception:  # noqa: BLE001 - thinking is best-effort
                    pass
                final_text = result.final_output
                if not isinstance(final_text, str):
                    final_text = str(final_text)
                usage = getattr(result, "usage", None)
                stop_reason = getattr(result, "stop_reason", None)
                if handle.cancel.is_set():
                    status = "cancelled"
                elif getattr(result, "interruptions", None):
                    status = "waiting_input"
                usage_meta: Any = {}
                if usage is not None:
                    try:
                        import dataclasses

                        usage_meta = (
                            dataclasses.asdict(usage)
                            if dataclasses.is_dataclass(usage)
                            else dict(usage)
                        )
                    except Exception:  # noqa: BLE001
                        usage_meta = {"raw": str(usage)}
                await self._emit(
                    handle,
                    self._stream_event(
                        handle,
                        "stage_end",
                        source=capability,
                        stage="responding",
                    ),
                )
                await self._emit(
                    handle,
                    self._stream_event(
                        handle,
                        "result",
                        content=final_text,
                        source=capability,
                        stage="result",
                        metadata={
                            "usage": usage_meta,
                            "stop_reason": str(stop_reason),
                            "agent_name": result.agent_name,
                        },
                    ),
                )
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - surface as turn failure
                log.exception("turn %s failed", handle.turn_id)
                status = "failed"
                error_info = {"message": str(exc), "retryable": True}
                await self._emit(
                    handle,
                    self._stream_event(
                        handle,
                        "error",
                        content=str(exc),
                        source=capability,
                        stage="responding",
                        metadata={"turn_terminal": True, "status": "failed"},
                    ),
                )
            finally:
                # Persist the assistant row + full event log for replay.
                turn_events = handle.events
                if status != "cancelled" or assistant_text_parts:
                    assistant_content = "".join(assistant_text_parts)
                    if not assistant_content and error_info is None:
                        # The SDK may produce output without text deltas
                        # (e.g. handoff-only turns); fall back to result text.
                        assistant_content = ""
                    self._store.append_assistant_message(
                        session,
                        assistant_content,
                        events=turn_events,
                        turn_id=handle.turn_id,
                        capability=capability,
                        metadata=error_info or {},
                    )
                self._store.finish_turn(session, handle.turn_id, status)
                done_status = status if status != "waiting_input" else "completed"
                await self._emit(
                    handle,
                    self._stream_event(
                        handle,
                        "done",
                        source=capability,
                        stage="done",
                        metadata={
                            "status": done_status,
                            "user_message_id": user_message["id"],
                        },
                    ),
                )
                handle.status = status
                self._store.save(session)

    # -- replay / subscription ------------------------------------------------

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
