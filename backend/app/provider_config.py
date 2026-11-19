"""DeepTutor-owned provider / model configuration.

This module is the *only* source of truth for which providers and models the
Settings UI features and which model a turn actually runs. Nothing here reads
``~/.vtx`` — the vtx SDK is used purely as an execution engine. Callers pass
the resolved provider / model / base_url / api_key into vtx explicitly (see
``agent/runtime.py``), so vtx never needs to consult its own config for model
selection.

Persistence layout (all under ``Settings.data_dir``):

* ``settings-catalog.json`` — connections, per-service profiles and their
  models, the active ``llm_selection`` and a short ``recent_models`` list.
* ``settings.json`` — legacy mirror of ``llm_selection`` kept in sync so older
  clients and the one-time importer keep working.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

SERVICES: tuple[str, ...] = (
    "llm", "task", "embedding", "search", "tts", "stt", "imagegen", "videogen",
)

_HEADERS = {"User-Agent": "DeepTutor/0.1 (+https://deeptutor.info)"}
_RECENT_LIMIT = 20
_PROBE_TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Built-in presets — DeepTutor's own static catalogue. No vtx import, ever.
# ---------------------------------------------------------------------------

# Each preset supplies the prefills the settings UI needs when adding a
# connection. Users can still add any custom provider with a custom base_url.
_BUILTIN: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_format": "openai_chat",
        "api_key_env": "OPENAI_API_KEY",
        "known_models": ["gpt-4o", "gpt-4o-mini", "o3-mini", "o4-mini"],
        "supports_vision": True,
        "supports_thinking": True,
        "services": ("llm", "embedding", "tts", "stt", "imagegen"),
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "api_format": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "known_models": [
            "claude-sonnet-4-20250514", "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
        ],
        "supports_vision": True,
        "supports_thinking": True,
        "services": ("llm",),
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_format": "openai_chat",
        "api_key_env": "OPENROUTER_API_KEY",
        "known_models": [
            "openai/gpt-4o", "anthropic/claude-3.7-sonnet",
            "google/gemini-2.5-pro", "deepseek/deepseek-chat",
        ],
        "supports_vision": True,
        "supports_thinking": True,
        "services": ("llm",),
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_format": "openai_chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "known_models": ["deepseek-chat", "deepseek-reasoner"],
        "supports_vision": False,
        "supports_thinking": True,
        "services": ("llm",),
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_format": "openai_chat",
        "api_key_env": "GEMINI_API_KEY",
        "known_models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "supports_vision": True,
        "supports_thinking": True,
        "services": ("llm",),
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_format": "openai_chat",
        "api_key_env": "GROQ_API_KEY",
        "known_models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
        "supports_vision": False,
        "supports_thinking": False,
        "services": ("llm",),
    },
    "mistral": {
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "api_format": "openai_chat",
        "api_key_env": "MISTRAL_API_KEY",
        "known_models": ["mistral-large-latest", "mistral-small-latest"],
        "supports_vision": False,
        "supports_thinking": False,
        "services": ("llm",),
    },
    "ollama": {
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "api_format": "openai_chat",
        "api_key_env": "",
        "known_models": ["llama3.1", "qwen2.5"],
        "supports_vision": False,
        "supports_thinking": False,
        "services": ("llm", "embedding"),
    },
    "xai": {
        "label": "xAI",
        "base_url": "https://api.x.ai/v1",
        "api_format": "openai_chat",
        "api_key_env": "XAI_API_KEY",
        "known_models": ["grok-3", "grok-3-mini"],
        "supports_vision": False,
        "supports_thinking": True,
        "services": ("llm",),
    },
    "zhipu": {
        "label": "Zhipu AI",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_format": "openai_chat",
        "api_key_env": "ZHIPU_API_KEY",
        "known_models": ["glm-4.5", "glm-4-plus"],
        "supports_vision": True,
        "supports_thinking": True,
        "services": ("llm", "embedding"),
    },
}

# Provider slugs whose wire protocol vtx must not infer from the model name.
_anthropic_slugs = {"anthropic", "aerolink"}
_responses_slugs = {"openai-responses", "codex", "openai-codex"}


def builtin_preset(slug: str | None) -> dict[str, Any] | None:
    if not slug:
        return None
    return _BUILTIN.get(slug.strip().lower())


def builtin_presets() -> dict[str, dict[str, Any]]:
    return _BUILTIN


def preset_api_key(slug: str | None) -> str:
    """API key for a built-in preset from its conventional env var."""
    return _env_key(builtin_preset(slug))


# ---------------------------------------------------------------------------
# Catalog IO — DeepTutor's own files
# ---------------------------------------------------------------------------

def catalog_path(settings: Settings) -> Path:
    return settings.data_dir / "settings-catalog.json"


def selection_path(settings: Settings) -> Path:
    return settings.data_dir / "settings.json"


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def default_catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "connections": [],
        "recent_models": [],
        "llm_selection": None,
        "services": {
            name: {"active_profile_id": None, "profiles": []} for name in SERVICES
        },
    }


def load_catalog(settings: Settings) -> dict[str, Any]:
    data = _read_json(catalog_path(settings), default_catalog())
    # Fill missing top-level keys so old catalogs keep working.
    defaults = default_catalog()
    for key, value in defaults.items():
        data.setdefault(key, value)
    services = data.setdefault("services", {})
    for name, svc in defaults["services"].items():
        bucket = services.setdefault(name, {})
        for key, value in svc.items():
            bucket.setdefault(key, value)
    return data


def save_catalog(settings: Settings, catalog: dict[str, Any]) -> None:
    _write_json(catalog_path(settings), catalog)


# ---------------------------------------------------------------------------
# Active LLM selection
# ---------------------------------------------------------------------------

def _clean_selection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    profile_id = str(value.get("profile_id") or "").strip()
    model_id = str(value.get("model_id") or "").strip()
    if not profile_id or not model_id:
        return None
    sel: dict[str, Any] = {"profile_id": profile_id, "model_id": model_id}
    effort = value.get("reasoning_effort")
    if effort:
        sel["reasoning_effort"] = effort
    return sel


def get_selection(settings: Settings, override: Any = None) -> dict[str, Any] | None:
    """Resolve the active selection: explicit override → catalog → legacy file."""
    sel = _clean_selection(override)
    if sel:
        return sel
    catalog = load_catalog(settings)
    sel = _clean_selection(catalog.get("llm_selection"))
    if sel:
        return sel
    sel = _clean_selection(_read_json(selection_path(settings), {}).get("llm_selection"))
    return sel


def set_selection(settings: Settings, selection: Any) -> dict[str, Any] | None:
    """Persist the active selection and update recency. Source of truth is the
    DeepTutor catalog; settings.json is mirrored for older clients."""
    sel = _clean_selection(selection)
    if sel is None:
        return None
    catalog = load_catalog(settings)
    catalog["llm_selection"] = sel
    recent = [item for item in (catalog.get("recent_models") or [])
              if not (isinstance(item, dict)
                      and item.get("profile_id") == sel["profile_id"]
                      and item.get("model_id") == sel["model_id"])]
    recent.insert(0, dict(sel))
    catalog["recent_models"] = recent[:_RECENT_LIMIT]
    save_catalog(settings, catalog)

    legacy = _read_json(selection_path(settings), {})
    legacy["llm_selection"] = sel
    _write_json(selection_path(settings), legacy)
    return sel


def recent_pairs(settings: Settings) -> set[tuple[str, str]]:
    catalog = load_catalog(settings)
    out: set[tuple[str, str]] = set()
    for item in catalog.get("recent_models") or []:
        if isinstance(item, dict) and item.get("profile_id") and item.get("model_id"):
            out.add((item["profile_id"], item["model_id"]))
    return out


# ---------------------------------------------------------------------------
# Runtime resolution — what actually gets sent to vtx
# ---------------------------------------------------------------------------

def _find_profile(catalog: dict[str, Any], profile_id: str) -> dict[str, Any] | None:
    for service in SERVICES:
        for profile in (catalog.get("services", {}).get(service, {}).get("profiles") or []):
            if profile.get("id") == profile_id:
                return profile
    return None


def _find_connection(catalog: dict[str, Any], connection_id: Any) -> dict[str, Any] | None:
    if not connection_id:
        return None
    for conn in catalog.get("connections") or []:
        if conn.get("id") == connection_id:
            return conn
    return None


def _connection_for_provider(catalog: dict[str, Any], provider: str) -> dict[str, Any] | None:
    for conn in catalog.get("connections") or []:
        if conn.get("provider") == provider:
            return conn
    return None


def _env_key(preset: dict[str, Any] | None) -> str:
    if not preset:
        return ""
    name = preset.get("api_key_env") or ""
    return os.environ.get(name, "").strip() if name else ""


def _vtx_provider_slug(provider: str, api_format: str | None) -> str:
    """Pick the provider label vtx uses to choose a wire protocol.

    Known slugs stay as-is; unknown custom endpoints are mapped from the
    declared API format so vtx does not guess the wrong SDK.
    """
    slug = (provider or "").strip() or "openai"
    fmt = (api_format or "auto").strip().lower()
    if fmt == "anthropic" and slug not in _anthropic_slugs:
        return "anthropic"
    if fmt == "openai_responses" and slug not in _responses_slugs:
        return "openai-responses"
    return slug


def resolve_runtime_model(
    settings: Settings,
    command: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return everything needed to build a vtx runtime, from DeepTutor data.

    Keys: provider, model, base_url, api_key, thinking_level, profile_id,
    model_id, source. ``provider`` is a vtx-compatible label; ``model`` is the
    wire model id. Never reads ~/.vtx.
    """
    command = command or {}
    selection = get_selection(settings, command.get("llm_selection"))
    catalog = load_catalog(settings)

    profile_id = str(selection.get("profile_id") if selection else "") or ""
    model_id = str(selection.get("model_id") if selection else "") or ""
    # Legacy/raw commands can still carry model_provider + model directly.
    provider_hint = str(command.get("model_provider") or "").strip()
    model_hint = str(command.get("model") or "").strip()

    preset = builtin_preset(profile_id)
    thinking = str((selection or {}).get("reasoning_effort") or "").strip() or None

    if profile_id and (profile := _find_profile(catalog, profile_id)):
        conn = _find_connection(catalog, profile.get("connection_id"))
        model_entry: dict[str, Any] = {}
        for m in profile.get("models") or []:
            if str(m.get("id")) == model_id:
                model_entry = m
                break
        provider_raw = str(
            profile.get("binding") or profile.get("provider")
            or (conn or {}).get("provider") or "openai"
        )
        base_url = str(profile.get("base_url") or (conn or {}).get("base_url") or "").strip()
        api_key = str(profile.get("api_key") or (conn or {}).get("api_key") or "").strip()
        api_format = profile.get("api_format") or (conn or {}).get("api_format")
        model = str(model_entry.get("model") or model_hint or model_id)
        thinking = thinking or str(model_entry.get("reasoning_effort") or "").strip() or None
        source = f"profile:{profile_id}"
    elif preset is not None:
        # A model chosen from the built-in preset list (e.g. env-key usage).
        conn = _connection_for_provider(catalog, profile_id)
        provider_raw = profile_id
        base_url = str((conn or {}).get("base_url") or preset.get("base_url") or "").strip()
        api_key = str((conn or {}).get("api_key") or _env_key(preset)).strip()
        api_format = preset.get("api_format")
        model = model_hint or model_id
        source = f"preset:{profile_id}"
    elif provider_hint:
        conn = _connection_for_provider(catalog, provider_hint)
        preset = builtin_preset(provider_hint)
        provider_raw = provider_hint
        base_url = str((conn or {}).get("base_url") or (preset or {}).get("base_url") or "").strip()
        api_key = str((conn or {}).get("api_key") or _env_key(preset)).strip()
        api_format = (preset or {}).get("api_format")
        model = model_hint or model_id or str(settings.default_model or "")
        source = "command"
    else:
        provider_raw = "openai"
        base_url, api_key, api_format = "", "", None
        model = model_hint or model_id or str(settings.default_model or "")
        source = "default"

    return {
        "provider": _vtx_provider_slug(provider_raw, api_format),
        "model": model,
        "base_url": base_url or None,
        "api_key": api_key or None,
        "thinking_level": thinking,
        "profile_id": profile_id,
        "model_id": model_id,
        "source": source,
    }


# ---------------------------------------------------------------------------
# UI-facing option builders
# ---------------------------------------------------------------------------

def _option(
    profile_id: str,
    model_id: str,
    *,
    profile_name: str,
    provider_label: str,
    model_name: str | None = None,
    supports_vision: bool = False,
    supports_thinking: bool = False,
    context_window: Any = None,
    reasoning_efforts: Any = None,
    declared_reasoning: bool = False,
    is_active_default: bool = False,
    is_recent: bool = False,
) -> dict[str, Any]:
    opt: dict[str, Any] = {
        "profile_id": profile_id,
        "model_id": model_id,
        "profile_name": profile_name,
        "model_name": model_name or model_id,
        "model": model_id,
        "provider": profile_id,
        "provider_label": provider_label,
        "supports_vision": bool(supports_vision),
        "supports_thinking": bool(supports_thinking),
        "is_active_default": is_active_default,
    }
    if isinstance(context_window, (int, float)) and context_window:
        opt["context_window"] = int(context_window)
    elif isinstance(context_window, str) and context_window.strip().isdigit():
        opt["context_window"] = int(context_window.strip())
    if isinstance(reasoning_efforts, list) and reasoning_efforts:
        opt["supported_reasoning_efforts"] = [str(x) for x in reasoning_efforts]
    if declared_reasoning:
        opt["declared_reasoning"] = True
    if is_recent:
        opt["is_recent"] = True
    return opt


def model_options(settings: Settings) -> list[dict[str, Any]]:
    """Every model the Settings/composer UI should feature, from our catalog.

    Saved profiles win; built-in presets fill in when a profile has no saved
    models so a fresh install can still pick a model.
    """
    catalog = load_catalog(settings)
    selection = get_selection(settings)
    recent = recent_pairs(settings)
    active_key = ""
    if selection:
        active_key = f"{selection['profile_id']}:{selection['model_id']}"
    options: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for profile in catalog.get("services", {}).get("llm", {}).get("profiles") or []:
        pid = str(profile.get("id") or "")
        if not pid:
            continue
        conn = _find_connection(catalog, profile.get("connection_id"))
        preset = builtin_preset(
            str(profile.get("binding") or (conn or {}).get("provider") or "")
        )
        label = str(
            profile.get("display_name") or profile.get("name")
            or (preset or {}).get("label") or pid
        )
        for m in profile.get("models") or []:
            mid = str(m.get("id") or "")
            if not mid:
                continue
            key = (pid, mid)
            if key in seen:
                continue
            seen.add(key)
            caps = m.get("capabilities") or {}
            if not isinstance(caps, dict):
                caps = {}
            efforts = m.get("supported_reasoning_efforts") or m.get("codex_supported_reasoning_levels")
            options.append(_option(
                pid, mid,
                profile_name=label,
                provider_label=label,
                model_name=str(m.get("name") or mid),
                supports_vision=bool(
                    caps.get("vision") or (preset or {}).get("supports_vision")
                ),
                supports_thinking=bool(
                    caps.get("reasoning") or (preset or {}).get("supports_thinking")
                ),
                context_window=m.get("context_window_tokens") or m.get("context_window"),
                reasoning_efforts=efforts,
                declared_reasoning=bool(caps.get("reasoning")),
                is_active_default=f"{pid}:{mid}" == active_key,
                is_recent=(pid, mid) in recent,
            ))

    # Preset fallback for slugs with no saved profile (env-key usage).
    for slug, preset in _BUILTIN.items():
        conn = _connection_for_provider(catalog, slug)
        if conn and any(
            str(p.get("binding") or p.get("provider") or "").lower() == slug
            for p in catalog.get("services", {}).get("llm", {}).get("profiles") or []
        ):
            continue
        for mid in preset.get("known_models") or []:
            key = (slug, str(mid))
            if key in seen:
                continue
            seen.add(key)
            options.append(_option(
                slug, str(mid),
                profile_name=str(preset.get("label") or slug),
                provider_label=str(preset.get("label") or slug),
                supports_vision=bool(preset.get("supports_vision")),
                supports_thinking=bool(preset.get("supports_thinking")),
                is_active_default=f"{slug}:{mid}" == active_key,
                is_recent=(slug, str(mid)) in recent,
            ))
    return options


def provider_options(settings: Settings) -> list[dict[str, Any]]:
    """Provider rows for the Connections page — our presets + saved ones."""
    catalog = load_catalog(settings)
    options: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(slug: str, label: str, base_url: str, default_model: str | None,
            vision: bool = False, thinking: bool = False) -> None:
        if slug in seen:
            return
        seen.add(slug)
        options.append({
            "value": slug,
            "label": label,
            "base_url": base_url,
            "default_model": default_model,
            "auth_mode": "api_key",
            "api_key_env": (builtin_preset(slug) or {}).get("api_key_env"),
            "supports_vision": bool(vision),
            "supports_thinking": bool(thinking),
            "status": "supported",
        })

    for conn in catalog.get("connections") or []:
        slug = str(conn.get("provider") or "").strip()
        if not slug:
            continue
        preset = builtin_preset(slug) or {}
        add(slug,
            str(conn.get("name") or preset.get("label") or slug),
            str(conn.get("base_url") or ""),
            None,
            bool(preset.get("supports_vision")),
            bool(preset.get("supports_thinking")))

    for slug, preset in _BUILTIN.items():
        known = preset.get("known_models") or []
        add(slug, str(preset.get("label") or slug),
            str(preset.get("base_url") or ""),
            str(known[0]) if known else None,
            bool(preset.get("supports_vision")),
            bool(preset.get("supports_thinking")))
    return options


def connection_targets(settings: Settings) -> list[dict[str, Any]]:
    """Per-provider prefills for the Connections page."""
    catalog = load_catalog(settings)
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(slug: str, label: str, base_url: str, models: list[str],
            services: tuple[str, ...]) -> None:
        if slug in seen:
            return
        seen.add(slug)
        targets.append({
            "provider": slug,
            "label": label,
            "default_base_url": base_url,
            "services": {
                service: {
                    "provider": slug,
                    "base_url": base_url,
                    "default_model": models[0] if models else "",
                }
                for service in services
            },
        })

    for conn in catalog.get("connections") or []:
        slug = str(conn.get("provider") or "").strip()
        if not slug:
            continue
        preset = builtin_preset(slug) or {}
        add(slug,
            str(conn.get("name") or preset.get("label") or slug),
            str(conn.get("base_url") or ""),
            list(preset.get("known_models") or []),
            tuple(preset.get("services") or ("llm",)))

    for slug, preset in _BUILTIN.items():
        add(slug, str(preset.get("label") or slug),
            str(preset.get("base_url") or ""),
            list(preset.get("known_models") or []),
            tuple(preset.get("services") or ("llm",)))
    return targets


def context_window_for(profile_id: str, model_id: str) -> int:
    """Context window for the composer's token bar, from our own catalog."""
    settings = get_settings()
    catalog = load_catalog(settings)
    profile = _find_profile(catalog, profile_id)
    if profile:
        for m in profile.get("models") or []:
            if str(m.get("id")) == model_id:
                value = m.get("context_window_tokens") or m.get("context_window")
                try:
                    return int(str(value).strip())
                except (TypeError, ValueError):
                    return 0
    return 0


# ---------------------------------------------------------------------------
# Live model discovery (no vtx fetchers, no ~/.vtx caches)
# ---------------------------------------------------------------------------

def _extract_ids(payload: Any) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        mid = str(value or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)

    if isinstance(payload, dict):
        for item in payload.get("data") or payload.get("models") or []:
            if isinstance(item, dict):
                add(item.get("id") or item.get("name") or item.get("model_id"))
            else:
                add(item)
    elif isinstance(payload, list):
        for item in payload:
            add(item.get("id") if isinstance(item, dict) else item)
    return ids


def probe_models(
    base_url: str,
    api_key: str | None = None,
    api_format: str | None = None,
) -> dict[str, Any]:
    """GET {base_url}/models directly. Returns status + model id list."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"status": "unreachable", "models": [], "http_status": None}
    if not base.lower().startswith(("http://", "https://")):
        return {"status": "invalid_url", "models": [], "http_status": None}

    headers = dict(_HEADERS)
    fmt = (api_format or "").strip().lower()
    if api_key:
        if fmt == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    url = f"{base}/models"
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError:
        return {"status": "unreachable", "models": [], "http_status": None}

    if response.status_code >= 400:
        return {
            "status": "unavailable",
            "models": [],
            "http_status": response.status_code,
        }
    try:
        ids = _extract_ids(response.json())
    except Exception:
        ids = []
    return {
        "status": "connected" if ids else "unavailable",
        "models": [{"id": m, "name": m} for m in ids[:200]],
        "http_status": response.status_code,
    }


def store_discovered_models(
    settings: Settings,
    provider: str,
    model_ids: list[str],
    *,
    connection_id: str | None = None,
) -> None:
    """Persist discovered model ids into the DeepTutor catalog."""
    if not provider or not model_ids:
        return
    catalog = load_catalog(settings)
    changed = False
    for conn in catalog.get("connections") or []:
        match = (connection_id and conn.get("id") == connection_id) or (
            not connection_id and conn.get("provider") == provider
        )
        if not match:
            continue
        conn.setdefault("discovery", {})["models"] = [
            {"id": m, "name": m} for m in model_ids
        ]
        conn["discovery"]["status"] = "connected"
        conn["discovery"]["checked_at"] = time.time()
        changed = True
    if changed:
        save_catalog(settings, catalog)


# Imported lazily at call sites that need a fresh Settings instance.
def get_settings() -> Settings:
    from .config import get_settings as _get_settings
    return _get_settings()
