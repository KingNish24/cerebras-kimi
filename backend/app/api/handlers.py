"""Internal handler registry — NOT mounted as HTTP routes.

These functions back the ACP ``http.request`` dispatch (app/api/dispatch.py).
The APIRouter below is never included in the app; it exists only as a route
table (path templates + methods + endpoints) for the dispatcher to match.

Implements the subset of DeepTutor's API the frontend needs to boot and hold a

Implements the subset of DeepTutor's API the frontend needs to boot and hold a
conversation: auth status, session index/detail/search, runtime status,
capability catalog, and LLM options. Everything else 404s cleanly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


# -- local shims (no FastAPI) ---------------------------------------------------


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: Any = None) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def Query(default: Any = None, **_kw: Any) -> Any:
    return default


class Request:  # annotation-only marker
    pass


class UploadFile:  # annotation-only marker
    pass


class _Route:
    def __init__(self, methods: list[str], path: str, endpoint: Any) -> None:
        self.methods = methods
        self.path = path
        self.endpoint = endpoint


class APIRouter:
    """Minimal route-table collector — decorators only, never mounted."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.routes: list[_Route] = []

    def _add(self, methods: list[str], path: str):
        def deco(fn):
            self.routes.append(_Route(methods, self.prefix + path, fn))
            return fn
        return deco

    def get(self, path: str): return self._add(["GET"], path)
    def post(self, path: str): return self._add(["POST"], path)
    def put(self, path: str): return self._add(["PUT"], path)
    def delete(self, path: str): return self._add(["DELETE"], path)
    def patch(self, path: str): return self._add(["PATCH"], path)
    def api_route(self, path: str, methods: list[str] | None = None):
        return self._add(methods or ["GET"], path)

from ..agent.runtime import AgentRuntime
from ..config import Settings
from ..store.session_store import SessionStore

router = APIRouter(prefix="/api")


def _store(request: Request) -> SessionStore:
    return request.app.state.store


def _runtime(request: Request) -> AgentRuntime:
    return request.app.state.runtime


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# -- auth -------------------------------------------------------------------


@router.get("/auth/status")
def auth_status(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    if settings.auth_enabled:
        # Auth is not implemented in this backend yet; report disabled-auth
        # shape so the UI does not lock users out of a local deployment.
        return {"enabled": True, "authenticated": False}
    return {
        "enabled": False,
        "authenticated": True,
        "user_id": "local",
        "username": "local",
        "role": "admin",
        "is_admin": True,
        "preset": None,
        "avatar": "",
        "learning_policy": None,
    }


@router.post("/auth/login")
def auth_login() -> dict[str, Any]:
    raise HTTPException(status_code=501, detail="auth is disabled on this backend")


# -- system / runtime ---------------------------------------------------------


@router.get("/system/runtime")
def system_runtime(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return {
        "worker_id": "local",
        "worker_count": 1,
        "coordination_mode": "memory",
        "redis_configured": False,
        "redis_status": "not_configured",
        "leader_id": None,
        "leader_healthy": None,
        "owner_turn_count": len(_runtime(request)._turns),
        "recovery_backlog": 0,
        "lease_ttl_seconds": 30,
        "renew_interval_seconds": 10,
        "recovery_interval_seconds": 5,
        "protocol_version": "2.0",
        "minimum_web_protocol_version": "2.0",
        "data_dir": str(settings.data_dir),
    }


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# -- sessions -----------------------------------------------------------------


def _summary(session: dict[str, Any]) -> dict[str, Any]:
    messages = session.get("messages", [])
    last = messages[-1] if messages else {}
    active = None
    turns = session.get("turns", {})
    running = [t for t in turns.values() if t.get("status") == "running"]
    if running:
        latest = max(running, key=lambda t: t.get("created_at", 0))
        # turn_id is the dict key; find it
        for tid, t in turns.items():
            if t is latest:
                active = tid
                break
    return {
        "id": session["id"],
        "session_id": session["id"],
        "content_workspace_id": (session.get("preferences") or {}).get("workspace_id"),
        "title": session.get("title") or "Untitled",
        "created_at": session.get("created_at", 0),
        "updated_at": session.get("updated_at", 0),
        "message_count": len(messages),
        "last_message": str(last.get("content", ""))[:200],
        "status": "running" if active else (session.get("status") or "idle"),
        "active_turn_id": active,
        "preferences": session.get("preferences") or {},
    }


@router.get("/sessions")
def list_sessions(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    sessions = _store(request).list(limit=limit, offset=offset)
    return {"sessions": [_summary(s) for s in sessions]}


@router.get("/sessions/search")
def search_sessions(
    request: Request,
    q: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    hits, total = _store(request).search(q, limit=limit, offset=offset)
    results = []
    for session in hits:
        summary = _summary(session)
        summary["match_excerpt"] = session.get("_match_excerpt", "")
        results.append(summary)
    return {"sessions": results, "total": total, "limit": limit, "offset": offset}


@router.get("/sessions/capabilities")
def session_capabilities() -> dict[str, Any]:
    return {"submission_replacement": False}


@router.get("/sessions/{session_id}")
def get_session(request: Request, session_id: str) -> dict[str, Any]:
    session = _store(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    summary = _summary(session)
    active_turns = []
    for tid, turn in session.get("turns", {}).items():
        active_turns.append(
            {
                "id": tid,
                "turn_id": tid,
                "session_id": session_id,
                "capability": "chat",
                "status": turn.get("status", "completed"),
                "error": "",
                "created_at": turn.get("created_at"),
                "updated_at": turn.get("finished_at"),
                "finished_at": turn.get("finished_at"),
                "last_seq": turn.get("last_seq", 0),
            }
        )
    return {
        **summary,
        "messages": session.get("messages", []),
        "active_turns": active_turns,
    }


class SessionPatch(BaseModel):
    title: str | None = None
    preferences: dict[str, Any] | None = None


@router.patch("/sessions/{session_id}")
@router.put("/sessions/{session_id}")
def update_session(
    request: Request, session_id: str, body: SessionPatch
) -> dict[str, Any]:
    store = _store(request)
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if body.title is not None:
        session["title"] = body.title.strip() or session.get("title") or "Untitled"
    if body.preferences is not None:
        session["preferences"] = {**(session.get("preferences") or {}), **body.preferences}
    store.save(session)
    return _summary(session)


@router.delete("/sessions/{session_id}")
def delete_session(request: Request, session_id: str) -> dict[str, Any]:
    if not _store(request).delete(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": True}


@router.delete("/sessions/{session_id}/messages/{message_id}")
def delete_message(request: Request, session_id: str, message_id: int) -> dict[str, Any]:
    store = _store(request)
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    before = len(session.get("messages", []))
    session["messages"] = [
        m for m in session.get("messages", []) if m.get("id") != message_id
    ]
    if len(session["messages"]) == before:
        raise HTTPException(status_code=404, detail="message not found")
    store.save(session)
    return {"deleted": True}


@router.get("/sessions/{session_id}/messages/{message_id}/events")
def message_events(
    request: Request,
    session_id: str,
    message_id: int,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    session = _store(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    message = next(
        (m for m in session.get("messages", []) if m.get("id") == message_id), None
    )
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    events = [
        e for e in message.get("events", []) if (e.get("seq") or 0) > after_seq
    ][:limit]
    return {
        "session_id": session_id,
        "message_id": message_id,
        "turn_id": (message.get("metadata") or {}).get("turn_id"),
        "events": events,
        "total": len(message.get("events", [])),
        "last_seq": max((e.get("seq") or 0 for e in message.get("events", [])), default=0),
        "next_seq": None,
        "complete": True,
    }


# -- capabilities ---------------------------------------------------------------


@router.get("/capabilities/registered")
def capabilities_registered() -> dict[str, Any]:
    return {
        "capabilities": [
            {
                "id": "chat",
                "kind": "capability",
                "available": True,
                "manifest": {
                    "name": "chat",
                    "description": "General tutoring conversation",
                    "version": "1.0.0",
                },
                "config_schema": {"type": "object", "properties": {}},
            }
        ]
    }


# -- settings -------------------------------------------------------------------


def _vtx_model_cache_entries(slug: str) -> dict[str, dict[str, Any]]:
    """``~/.vtx/models/<slug>.json`` entries keyed by model id.

    Both vtx fetchers write this file; entries carry context_window,
    max_tokens, supports_thinking, thinking_level_map, supports_images —
    exactly the capability data the settings UI needs.
    """
    try:
        import json as _json
        from pathlib import Path

        cache = Path.home() / ".vtx" / "models" / f"{slug}.json"
        if not cache.exists():
            return {}
        data = _json.loads(cache.read_text(encoding="utf-8"))
        out: dict[str, dict[str, Any]] = {}
        for m in data.get("models", []):
            if isinstance(m, dict) and m.get("id"):
                out[str(m["id"])] = m
        return out
    except Exception:  # noqa: BLE001
        return {}


def _vtx_model_capability_fields(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Map a vtx cache entry to LLMOption capability fields."""
    if not entry:
        return {}
    fields: dict[str, Any] = {}
    cw = entry.get("context_window") or entry.get("context_length")
    if isinstance(cw, (int, float)) and cw:
        fields["context_window"] = int(cw)
    tlm = entry.get("thinking_level_map")
    if isinstance(tlm, dict) and tlm:
        levels = [k for k, v in tlm.items() if v is not None]
        if levels:
            fields["supported_reasoning_efforts"] = levels
            fields["declared_reasoning"] = True
    elif entry.get("supports_thinking"):
        fields["declared_reasoning"] = True
    if entry.get("supports_images"):
        fields["supports_vision"] = True
    return fields


def _vtx_provider_model_ids(slug: str, provider: Any) -> list[str]:
    """All model ids vtx knows for a provider: static known_models plus the
    fetched caches under ``~/.vtx/models/<slug>.json`` (model_fetcher and
    dynamic_models both write there — same files the vtx TUI reads)."""
    ids: list[str] = []
    seen: set[str] = set()

    def _add(mid: Any) -> None:
        mid = str(mid or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)

    for m in getattr(provider, "known_models", None) or []:
        _add(m)
    # Read vtx's on-disk model caches directly: both model_fetcher and
    # dynamic_models write ~/.vtx/models/<slug>.json as
    # {fetched_at, models: [{id, name, ...}]}. Reading the file avoids
    # vtx's fetch-on-stale path, which would block this request on network.
    try:
        import json as _json
        from pathlib import Path

        cache = Path.home() / ".vtx" / "models" / f"{slug}.json"
        if cache.exists():
            data = _json.loads(cache.read_text(encoding="utf-8"))
            for m in data.get("models", []):
                if isinstance(m, dict):
                    _add(m.get("id") or m.get("model_id"))
                else:
                    _add(m)
    except Exception:  # noqa: BLE001 - caches are best-effort
        pass
    return ids


def _vtx_model_options() -> list[dict[str, Any]]:
    """Enumerate the vtx provider catalog into LLMOption rows."""
    options: list[dict[str, Any]] = []
    try:
        from vtx.ai.provider_catalog import list_providers

        for p in list_providers():
            models = _vtx_provider_model_ids(p.slug, p)[:100]
            if not models:
                continue
            cache = _vtx_model_cache_entries(p.slug)
            for m in models:
                option = {
                    "profile_id": p.slug,
                    "model_id": m,
                    "profile_name": getattr(p, "display_name", p.slug),
                    "model_name": m,
                    "model": m,
                    "provider": p.slug,
                    "provider_label": getattr(p, "display_name", p.slug),
                    "supports_vision": bool(getattr(p, "supports_vision", False)),
                    "supports_thinking": bool(getattr(p, "supports_thinking", False)),
                    "is_active_default": False,
                }
                # Per-model capability data comes from vtx's own cache entry
                # (context window, reasoning levels, vision) — same source
                # the vtx TUI reads, not a frontend-side guess.
                option.update(_vtx_model_capability_fields(cache.get(m)))
                options.append(option)
    except Exception:  # noqa: BLE001
        pass
    return options


def _saved_selection(request: Request) -> dict[str, Any] | None:
    path = request.app.state.settings.data_dir / "settings.json"
    try:
        import json as _json

        data = _json.loads(path.read_text(encoding="utf-8"))
        sel = data.get("llm_selection")
        return sel if isinstance(sel, dict) else None
    except Exception:
        return None


@router.get("/settings/llm-options")
def llm_options(request: Request) -> dict[str, Any]:
    options = _vtx_model_options()
    saved = _saved_selection(request)
    # vtx's own last_selected is the shared source of truth with the CLI.
    try:
        from vtx.core.config import get_last_selected

        last = get_last_selected()
        if last.provider and last.model_id:
            saved = {
                "profile_id": last.provider,
                "model_id": last.model_id,
                "reasoning_effort": last.thinking_level,
            }
    except Exception:  # noqa: BLE001
        pass
    active = None
    if saved and saved.get("profile_id") and saved.get("model_id"):
        active = {
            "profile_id": saved["profile_id"],
            "model_id": saved["model_id"],
            "reasoning_effort": saved.get("reasoning_effort"),
        }
        for opt in options:
            if opt["profile_id"] == saved["profile_id"] and opt["model_id"] == saved["model_id"]:
                opt["is_active_default"] = True
                break
    if active is None and options:
        active = {
            "profile_id": options[0]["profile_id"],
            "model_id": options[0]["model_id"],
            "reasoning_effort": None,
        }
        options[0]["is_active_default"] = True
    # Mark models from vtx's recent_models list (~/.vtx/config.yml) so the
    # composer dropdown can show just what the user actually picks — the
    # same recency the vtx TUI /model picker uses.
    try:
        from vtx.core.config import get_recent_models

        recent = {(p, m) for p, m in get_recent_models()}
        for opt in options:
            if (opt["profile_id"], opt["model_id"]) in recent:
                opt["is_recent"] = True
    except Exception:  # noqa: BLE001
        pass
    return {"active": active, "options": options}


class ProviderApply(BaseModel):
    profile_id: str | None = None
    model_id: str | None = None
    llm_selection: dict[str, Any] | None = None


@router.post("/settings/apply/provider")
def apply_provider(request: Request, body: ProviderApply) -> dict[str, Any]:
    import json as _json

    sel = body.llm_selection or (
        {"profile_id": body.profile_id, "model_id": body.model_id}
        if body.profile_id and body.model_id
        else None
    )
    if sel:
        # Mirror into ~/.vtx/config.yml exactly like the vtx TUI /model picker:
        # last_selected + recent_models, so the CLI and this backend agree.
        try:
            from vtx.core.config import add_recent_model, set_last_selected

            provider = sel.get("profile_id")
            model_id = sel.get("model_id")
            if provider and model_id:
                set_last_selected(
                    model_id=model_id,
                    provider=provider,
                    thinking_level=sel.get("reasoning_effort"),
                )
                add_recent_model(provider, model_id)
        except Exception:  # noqa: BLE001 - vtx config write is best-effort
            pass
        path = request.app.state.settings.data_dir / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data["llm_selection"] = sel
        path.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "llm_selection": sel}


class ProviderProbe(BaseModel):
    binding: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_version: str | None = None
    api_format: str | None = None
    service: str | None = None
    profile_id: str | None = None
    connection_id: str | None = None
    extra_headers: Any = None
    proxy: str | None = None


@router.post("/settings/test-provider")
def test_provider(request: Request, body: ProviderProbe) -> dict[str, Any]:
    """Probe a provider connection and return a Discovery payload.

    Uses vtx's own fetchers so the model list lands in ``~/.vtx/models/`` —
    the same cache the vtx TUI reads. ``binding`` is the provider slug.
    """
    service = (body.service or "llm").strip()
    slug = (body.binding or "").strip()
    provider = _vtx_provider_for(slug or None, body.base_url)
    resolved_slug = getattr(provider, "slug", None) or slug

    # Persist a typed key into vtx's auth store so probes AND turns can use it.
    if resolved_slug and body.api_key:
        try:
            from vtx.ai.oauth import dynamic as vtx_dynamic

            vtx_dynamic.save_api_key(resolved_slug, body.api_key)
        except Exception:  # noqa: BLE001
            pass

    base = (body.base_url or (getattr(provider, "base_url", "") if provider else "") or "").strip()
    if base and not base.lower().startswith(("http://", "https://")):
        return {"status": "invalid_url", "models": []}

    # vtx providers are LLM-shaped; other services have no catalog here.
    if service not in ("llm", "task"):
        return {
            "status": "unavailable",
            "models": [],
            "capabilities": [],
            "http_status": None,
        }

    if not resolved_slug and not base:
        return {"status": "unreachable", "models": []}

    # Refresh through vtx (writes ~/.vtx/models/<slug>.json), then read back.
    status = "connected"
    http_status: int | None = None
    try:
        if resolved_slug:
            from vtx.ai import dynamic_models, model_fetcher

            try:
                dynamic_models.refresh_provider(resolved_slug)
            except Exception:  # noqa: BLE001 - not a dynamic provider
                pass
            try:
                model_fetcher.refresh_provider_models(resolved_slug)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        status = "unreachable"

    ids = _vtx_provider_model_ids(resolved_slug, provider) if resolved_slug else []
    if not ids:
        # No listable models — connection may still be valid for manual IDs.
        status = "unavailable" if status == "connected" else status

    models = [{"id": m, "name": m} for m in ids[:200]]
    return {
        "status": status,
        "models": models,
        "capabilities": (
            [{"category": "llm", "evidence": "metadata"}] if status == "connected" else []
        ),
        "checked_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "http_status": http_status,
    }


@router.get("/settings/model-capabilities")
def model_capabilities() -> dict[str, Any]:
    return {"capabilities": {}}


class FetchModelsBody(BaseModel):
    binding: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    profile_id: str | None = None
    service: str | None = None
    api_format: str | None = None


def _vtx_provider_for(binding: str | None, base_url: str | None):
    """Find the vtx catalog provider matching a connection binding/base_url."""
    try:
        from vtx.ai.provider_catalog import list_providers

        providers = list(list_providers())
        if binding:
            for p in providers:
                if p.slug == binding:
                    return p
        if base_url:
            norm = base_url.rstrip("/")
            for p in providers:
                if (getattr(p, "base_url", "") or "").rstrip("/") == norm:
                    return p
    except Exception:  # noqa: BLE001
        pass
    return None


@router.post("/settings/fetch-models")
def fetch_models(body: FetchModelsBody) -> dict[str, Any]:
    """Discover models for a provider connection.

    Resolution order: vtx catalog ``known_models`` → live ``GET {base_url}/models``.
    """
    provider = _vtx_provider_for(body.binding, body.base_url)
    slug = getattr(provider, "slug", None) or (body.binding or "").strip()

    # Persist the user's api_key/base_url override into vtx's dynamic-auth
    # store so the refresh below can authenticate — same file vtx reads.
    if slug and (body.api_key or body.base_url):
        try:
            from vtx.ai.oauth import dynamic as vtx_dynamic

            if body.api_key:
                vtx_dynamic.save_api_key(slug, body.api_key)
        except Exception:  # noqa: BLE001
            pass

    # Refresh through vtx's own fetchers — they write ~/.vtx/models/<slug>.json
    # in the same CachedCatalog format the vtx TUI reads.
    refreshed = False
    if slug:
        try:
            from vtx.ai import dynamic_models, model_fetcher

            try:
                n = dynamic_models.refresh_provider(slug)
                refreshed = refreshed or n > 0
            except Exception:  # noqa: BLE001 - not a dynamic provider
                pass
            try:
                n = model_fetcher.refresh_provider_models(slug)
                refreshed = refreshed or n > 0
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    # Read back through the same cached path vtx uses (no extra network).
    ids = _vtx_provider_model_ids(slug, provider) if slug else []
    if not ids and provider is not None:
        ids = [str(m) for m in (getattr(provider, "known_models", None) or [])]
    return {"models": [{"id": m, "name": m} for m in ids[:200]], "refreshed": refreshed}


def _vtx_provider_options() -> list[dict[str, Any]]:
    """vtx provider catalog → ProviderOption rows for the connections page."""
    options: list[dict[str, Any]] = []
    try:
        from vtx.ai.provider_catalog import list_providers

        for p in list_providers():
            known = list(getattr(p, "known_models", None) or [])
            api_key_env = getattr(p, "api_key_env", None)
            options.append(
                {
                    "value": p.slug,
                    "label": getattr(p, "display_name", p.slug),
                    "base_url": getattr(p, "base_url", None),
                    "default_model": known[0] if known else None,
                    "auth_mode": "api_key" if api_key_env else "api_key",
                    "api_key_env": api_key_env,
                    "supports_vision": bool(getattr(p, "supports_vision", False)),
                    "supports_thinking": bool(getattr(p, "supports_thinking", False)),
                    "status": "supported",
                }
            )
    except Exception:  # noqa: BLE001
        pass
    return options


def _vtx_connection_targets() -> list[dict[str, Any]]:
    """Per-provider prefills the connections UI needs (base_url, default model).

    Every vtx catalog provider is LLM-shaped (openai_compat or anthropic),
    so each gets an ``llm`` service entry — same shape DeepTutor emits.
    """
    targets: list[dict[str, Any]] = []
    try:
        from vtx.ai.provider_catalog import list_providers

        for p in list_providers():
            known = list(getattr(p, "known_models", None) or [])
            base = getattr(p, "base_url", "") or ""
            if not base and not known:
                continue
            targets.append(
                {
                    "provider": p.slug,
                    "label": getattr(p, "display_name", p.slug),
                    "default_base_url": base,
                    "services": {
                        "llm": {
                            "provider": p.slug,
                            "base_url": base,
                            "default_model": known[0] if known else "",
                        }
                    },
                }
            )
    except Exception:  # noqa: BLE001
        pass
    return targets


@router.get("/settings")
def settings_root(request: Request) -> dict[str, Any]:
    providers = _vtx_provider_options()
    return {
        "providers": {
            "llm": providers,
            "task": [],
            "embedding": [],
            "search": [],
            "tts": [],
            "stt": [],
            "imagegen": [],
            "videogen": [],
        },
        "connection_targets": _vtx_connection_targets(),
        "task_kinds": [],
        "ui": {"theme": "dark", "language": "en", "response_language": "en"},
        "services": {},
        "catalog": _load_live_catalog(request),
    }



# -- knowledge bases ---------------------------------------------------------


@router.get("/knowledge-bases")
def kb_list(request: Request) -> dict[str, Any]:
    return {"knowledge_bases": request.app.state.kb.list()}


class KBCreate(BaseModel):
    name: str


@router.post("/knowledge-bases")
def kb_create(request: Request, body: KBCreate) -> dict[str, Any]:
    return request.app.state.kb.create(body.name)


@router.delete("/knowledge-bases/{name}")
def kb_delete(request: Request, name: str) -> dict[str, Any]:
    if not request.app.state.kb.delete(name):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return {"deleted": True}


@router.get("/knowledge-bases/{name}/files")
def kb_files_list(request: Request, name: str) -> dict[str, Any]:
    if not request.app.state.kb.exists(name):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return {"files": request.app.state.kb.list_documents(name)}


@router.post("/knowledge-bases/{name}/upload")
async def kb_upload(request: Request, name: str, file: UploadFile) -> dict[str, Any]:
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large")
    return request.app.state.kb.add_document(name, file.filename or "upload.bin", data)


# -- boot-time stubs -----------------------------------------------------------


def _settings_dir(request: Request) -> "Path":
    from pathlib import Path

    d = request.app.state.settings.data_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _default_catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "connections": [],
        "services": {
            name: {"active_profile_id": None, "profiles": []}
            for name in ("llm", "task", "embedding", "search", "tts", "stt", "imagegen", "videogen")
        },
    }


def _load_live_catalog(request: Request) -> dict[str, Any]:
    import json as _json
    from pathlib import Path

    path = _settings_dir(request) / "settings-catalog.json"
    data = None
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "services" in raw:
            data = raw
    except Exception:
        pass
    if data is None:
        return _default_catalog()
    # Self-heal: connections saved before connection_targets existed can have
    # an empty base_url, which makes the UI's model-sync guard skip fetching.
    # Backfill from the vtx provider catalog (same source connection_targets uses).
    try:
        from vtx.ai.provider_catalog import list_providers

        by_slug = {p.slug: p for p in list_providers()}
        dirty = False
        for conn in data.get("connections", []) or []:
            if conn.get("base_url"):
                continue
            p = by_slug.get(conn.get("provider") or "")
            base = getattr(p, "base_url", "") if p else ""
            if base:
                conn["base_url"] = base
                dirty = True
        if dirty:
            _save_live_catalog(request, data)
    except Exception:  # noqa: BLE001
        pass
    return data


def _save_live_catalog(request: Request, catalog: dict[str, Any]) -> None:
    import json as _json

    path = _settings_dir(request) / "settings-catalog.json"
    path.write_text(_json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")


def _load_draft(request: Request) -> dict[str, Any] | None:
    import json as _json

    path = _settings_dir(request) / "settings-draft.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "catalog" in data else None
    except Exception:
        return None


def _save_draft(request: Request, draft: dict[str, Any] | None) -> None:
    import json as _json
    from pathlib import Path

    path = _settings_dir(request) / "settings-draft.json"
    if draft is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(_json.dumps(draft, ensure_ascii=False), encoding="utf-8")


@router.get("/settings/draft")
def settings_draft_get(request: Request) -> dict[str, Any]:
    return {"draft": _load_draft(request)}


@router.put("/settings/draft")
def settings_draft_put(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    stored = {"catalog": body.get("catalog"), "extensions": body.get("extensions") or {}}
    _save_draft(request, stored)
    return {"draft": stored}


@router.post("/settings/draft")
def settings_draft_post(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return settings_draft_put(request, body)


@router.delete("/settings/draft")
def settings_draft_delete(request: Request) -> dict[str, Any]:
    _save_draft(request, None)
    return {"ok": True}


def _svc(catalog: dict[str, Any], name: str) -> dict[str, Any]:
    services = catalog.setdefault("services", {})
    return services.setdefault(name, {"active_profile_id": None, "profiles": []})


def _find_profile(catalog: dict[str, Any], service: str, profile_id: str):
    for p in _svc(catalog, service).get("profiles", []):
        if p.get("id") == profile_id:
            return p
    return None


def _apply_registry_edit(catalog: dict[str, Any], edit: dict[str, Any]) -> None:
    """Mirror of the frontend's stageRegistryAction + field updates."""
    kind = edit.get("kind")
    ref = edit.get("ref") or {}
    service = edit.get("service")

    if kind == "task_choice" and edit.get("task"):
        task = _svc(catalog, "task")
        t = edit["task"]
        task_kind = t.get("task_kind")
        choice = {k: v for k, v in t.items() if k != "task_kind"}
        if task_kind:
            overrides = dict(task.get("overrides") or {})
            if choice.get("mode") == "global":
                overrides.pop(task_kind, None)
            else:
                overrides[task_kind] = choice
            if overrides:
                task["overrides"] = overrides
            else:
                task.pop("overrides", None)
        else:
            task.update(choice)
        return

    if kind == "provider" and edit.get("delete") and ref:
        if ref.get("connection_id"):
            catalog["connections"] = [
                c for c in catalog.get("connections", []) if c.get("id") != ref["connection_id"]
            ]
        elif ref.get("service") and ref.get("profile_id"):
            bucket = _svc(catalog, ref["service"])
            bucket["profiles"] = [p for p in bucket.get("profiles", []) if p.get("id") != ref["profile_id"]]
        return

    if kind == "provider" and ref:
        # Field-level update on a connection or profile.
        fields = edit.get("fields") or {}
        if ref.get("connection_id"):
            for c in catalog.get("connections", []):
                if c.get("id") == ref["connection_id"]:
                    c.update(fields)
                    if edit.get("config"):
                        c.update(edit["config"])
                    return
        elif ref.get("service") and ref.get("profile_id"):
            profile = _find_profile(catalog, ref["service"], ref["profile_id"])
            if profile:
                profile.update(fields)
                if edit.get("config"):
                    profile.update(edit["config"])
        return

    if not service:
        return
    bucket = _svc(catalog, service)
    profile = _find_profile(catalog, service, edit.get("profile_id") or "")

    if kind == "default" and profile is not None:
        bucket["active_profile_id"] = profile["id"]
        bucket["active_model_id"] = edit.get("model_id")
        if service == "task":
            bucket["mode"] = "profiles"
        return

    if profile is None:
        return

    if kind == "model":
        model = edit.get("model")
        if edit.get("delete"):
            mid = edit.get("model_id") or (model or {}).get("id")
            profile["models"] = [m for m in profile.get("models", []) if m.get("id") != mid]
            if not profile.get("models") and profile.get("provider_ref"):
                bucket["profiles"] = [p for p in bucket["profiles"] if p is not profile]
        elif model:
            models = profile.setdefault("models", [])
            existing = next((i for i, m in enumerate(models) if m.get("id") == model.get("id")), None)
            if existing is None:
                models.append(model)
            else:
                models[existing] = {**models[existing], **model}
        return

    if edit.get("config"):
        profile.update(edit["config"])


@router.post("/settings/apply/registry")
def apply_registry(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    catalog = _load_live_catalog(request)
    _apply_registry_edit(catalog, body)
    _save_live_catalog(request, catalog)
    _save_draft(request, None)
    return {"catalog": catalog}


@router.post("/settings/apply/service")
def apply_service(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    catalog = _load_live_catalog(request)
    service = body.get("service")
    config = body.get("config")
    if service in catalog.get("services", {}):
        catalog["services"][service] = config
    _save_live_catalog(request, catalog)
    return {"catalog": catalog, "draft": _load_draft(request)}


@router.post("/settings/apply")
def apply_catalog(request: Request) -> dict[str, Any]:
    draft = _load_draft(request)
    catalog = draft.get("catalog") if draft else _load_live_catalog(request)
    if catalog:
        _save_live_catalog(request, catalog)
    _save_draft(request, None)
    return {"catalog": _load_live_catalog(request), "draft": None}


@router.get("/settings/display")
def settings_display_get() -> dict[str, Any]:
    return {}


@router.post("/settings/display")
def settings_display_post() -> dict[str, Any]:
    return {"ok": True}


@router.get("/modules")
def modules_list() -> dict[str, Any]:
    return {"modules": []}


@router.get("/settings/page-customization")
def page_customization() -> dict[str, Any]:
    return {
        "chat": {
            "hidden_modes": [],
            "controls": {
                "skills": True,
                "mcp": True,
                "partners": True,
                "preferences": True,
                "voice": True,
                "create": True,
            },
            "starters": {},
        },
        "learning": {"hidden_features": []},
    }


@router.get("/settings/workspace/registrations")
def workspace_registrations() -> dict[str, Any]:
    return {"registrations": []}


@router.get("/settings/workspace/folders")
def workspace_folders(path: str = "") -> dict[str, Any]:
    return {"name": "", "path": "", "roots": [], "parent": None, "folders": [], "truncated": False}


@router.get("/system/status")
def system_status() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/reading/workspaces/index")
def reading_workspaces_index() -> dict[str, Any]:
    return {"workspaces": []}


@router.get("/mastery-paths/topics/index")
def mastery_topics_index() -> dict[str, Any]:
    return {"topics": []}


@router.get("/tools")
def tools_settings() -> dict[str, Any]:
    return {
        "enabled_optional_tools": [
            "brainstorm", "web_search", "paper_search", "zotero_search",
            "reason", "imagegen", "videogen",
        ]
    }


@router.get("/subagents/settings")
def subagents_settings() -> dict[str, Any]:
    return {}


@router.put("/subagents/settings")
def subagents_settings_put() -> dict[str, Any]:
    return {}


@router.get("/dashboard/suggestions")
def dashboard_suggestions() -> dict[str, Any]:
    return {}


@router.post("/dashboard/suggestions/refresh")
def dashboard_suggestions_refresh() -> dict[str, Any]:
    return {}


@router.get("/knowledge-bases/list")
def kb_list_alias(request: Request, resource_library: bool = False) -> list[dict[str, Any]]:
    return request.app.state.kb.list()


# Catch-all for settings sub-pages/extensions the UI probes (GET/PUT/POST).
# Specific routes above always win; this only silences 404 noise.
@router.api_route(
    "/settings/{path:path}",
    methods=["GET", "PUT", "POST", "DELETE", "PATCH"],
)
def settings_catchall(path: str) -> dict[str, Any]:
    return {}


@router.get("/sessions/{session_id}/organization")
def session_organization(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "folders": [], "tags": []}


@router.get("/sessions/{session_id}/threads")
def session_threads(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "threads": []}
