"""Search across chat sessions, with optional embedding retrieval.

Dito can refer back to anything the learner and the tutor discussed, not just
the page it is currently beside. This module searches every persisted session
(``data/sessions/*.json``) and returns the matching sessions with a snippet of
the exchange.

Retrieval is lexical by default (term overlap, phrase bonus, recency) so it
works with no embedding provider configured. When an embedding model is
configured under Settings → Embedding, message embeddings are cached beside the
sessions and the query is embedded as well; the two scores are combined.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .. import provider_config
from ..config import Settings

_CACHE_NAME = "session-search-embeddings.json"
_MAX_MESSAGE_CHARS = 4000
_EMBED_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_TOKEN = re.compile(r"[a-zA-Z0-9_]+")
_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does", "for",
    "from", "how", "i", "in", "is", "it", "its", "me", "my", "of", "on", "or",
    "that", "the", "this", "to", "was", "what", "when", "where", "which", "who",
    "why", "will", "with", "you", "your",
}


def _tokens(text: str) -> list[str]:
    return [t for t in (m.group(0).lower() for m in _TOKEN.finditer(text)) if t not in _STOP]


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return ""


def _session_files(settings: Settings) -> list[Path]:
    root = settings.sessions_dir
    if not root.exists():
        return []
    return sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def _load_session(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _exchanges(session: dict[str, Any]) -> list[dict[str, Any]]:
    """User/assistant messages as searchable exchanges (user + next reply)."""
    messages = [m for m in session.get("messages") or [] if isinstance(m, dict)]
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(message).strip()
        if not text:
            continue
        reply = ""
        if role == "user" and index + 1 < len(messages):
            following = messages[index + 1]
            if following.get("role") == "assistant":
                reply = _message_text(following).strip()
        out.append(
            {
                "id": str(message.get("id")),
                "role": role,
                "text": text[:_MAX_MESSAGE_CHARS],
                "reply": reply[:_MAX_MESSAGE_CHARS],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Lexical scoring
# ---------------------------------------------------------------------------


def _lexical_score(query_terms: list[str], text: str) -> float:
    if not query_terms:
        return 0.0
    lowered = text.lower()
    terms = _tokens(lowered)
    if not terms:
        return 0.0
    counts: dict[str, int] = {}
    for term in terms:
        counts[term] = counts.get(term, 0) + 1
    score = 0.0
    for term in query_terms:
        hits = counts.get(term, 0)
        if hits:
            score += 1.0 + math.log1p(hits)
    # Reward the exact phrase so "depletion region" beats two scattered words.
    phrase = " ".join(query_terms)
    if len(query_terms) > 1 and phrase in lowered:
        score += 2.0
    return score


def _snippet(text: str, query_terms: list[str], width: int = 260) -> str:
    lowered = text.lower()
    index = -1
    for term in query_terms:
        index = lowered.find(term)
        if index >= 0:
            break
    if index < 0:
        return text[:width].replace("\n", " ")
    start = max(0, index - width // 3)
    return text[start : start + width].replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Embeddings (optional)
# ---------------------------------------------------------------------------


def _embedding_target(settings: Settings) -> dict[str, Any] | None:
    """The configured embedding profile/model, or ``None`` when absent."""
    catalog = provider_config.load_catalog(settings)
    service = catalog.get("services", {}).get("embedding") or {}
    profiles = [p for p in service.get("profiles") or [] if p.get("models")]
    if not profiles:
        return None
    active = str(service.get("active_profile_id") or "")
    profile = next((p for p in profiles if p.get("id") == active), profiles[0])
    model_entry = next(iter(profile.get("models") or []), {})
    conn = provider_config._find_connection(
        catalog, profile.get("connection_id")
    ) or provider_config._find_connection(
        catalog, (model_entry.get("provider_ref") or {}).get("connection_id")
    )
    base_url = str(profile.get("base_url") or (conn or {}).get("base_url") or "").strip()
    api_key = str(profile.get("api_key") or (conn or {}).get("api_key") or "").strip()
    model = str(model_entry.get("model") or model_entry.get("id") or "")
    if not (base_url and model):
        return None
    return {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model}


async def _embed(target: dict[str, Any], texts: list[str]) -> list[list[float]] | None:
    headers = {"Content-Type": "application/json"}
    if target.get("api_key"):
        headers["Authorization"] = f"Bearer {target['api_key']}"
    body = {"model": target["model"], "input": texts}
    try:
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT) as client:
            response = await client.post(f"{target['base_url']}/embeddings", headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
    except Exception:  # noqa: BLE001 - retrieval must degrade to lexical
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
    vectors = [row.get("embedding") for row in ordered]
    if not all(isinstance(v, list) and v for v in vectors):
        return None
    return vectors


def _cache_path(settings: Settings) -> Path:
    return settings.data_dir / "session-search" / _CACHE_NAME


def _load_cache(settings: Settings) -> dict[str, Any]:
    try:
        data = json.loads(_cache_path(settings).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(settings: Settings, cache: dict[str, Any]) -> None:
    path = _cache_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(path)


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


async def _semantic_scores(
    settings: Settings, cache: dict[str, Any], items: list[dict[str, Any]], query: str
) -> dict[str, float] | None:
    target = _embedding_target(settings)
    if target is None or not items:
        return None
    key_prefix = f"{target['model']}:"
    missing = [
        item for item in items
        if cache.get(key_prefix + item["key"], {}).get("hash") != item["hash"]
    ]
    if missing:
        vectors = await _embed(target, [item["text"] for item in missing])
        if vectors is None:
            return None
        for item, vector in zip(missing, vectors):
            cache[key_prefix + item["key"]] = {"hash": item["hash"], "vec": vector}
    query_vector = await _embed(target, [query])
    if not query_vector:
        return None
    qvec = query_vector[0]
    scores: dict[str, float] = {}
    for item in items:
        entry = cache.get(key_prefix + item["key"]) or {}
        vector = entry.get("vec")
        if isinstance(vector, list) and vector:
            scores[item["key"]] = _cosine(qvec, vector)
    return scores


# ---------------------------------------------------------------------------
# Public search
# ---------------------------------------------------------------------------


def search(
    settings: Settings,
    query: str,
    *,
    limit: int = 5,
    page_id: str = "",
) -> list[dict[str, Any]]:
    """Synchronous lexical search; ``page_id`` limits to one notebook's threads."""
    needle = (query or "").strip()
    if not needle:
        return []
    terms = _tokens(needle)
    limit = max(1, min(int(limit or 5), 20))
    hits: list[dict[str, Any]] = []
    for path in _session_files(settings):
        session = _load_session(path)
        if not session:
            continue
        preferences = session.get("preferences") or {}
        if page_id and preferences.get("note_document_id") != page_id:
            continue
        best_score = 0.0
        best_exchange: dict[str, Any] | None = None
        title = str(session.get("title") or "")
        if terms and _lexical_score(terms, title):
            best_score = _lexical_score(terms, title)
        for exchange in _exchanges(session):
            score = _lexical_score(terms, exchange["text"] + " " + exchange["reply"])
            # Prefer the actual exchange over the title on a tie so the
            # rendered result carries the conversation snippet, not just a
            # repeat of the title.
            if score > 0 and score >= best_score:
                best_score, best_exchange = score, exchange
        if best_score <= 0:
            continue
        exchange = best_exchange or {"text": "", "reply": ""}
        hits.append(
            {
                "session_id": str(session.get("id") or path.stem),
                "title": title or "Untitled conversation",
                "updated_at": float(session.get("updated_at") or 0),
                "page_id": str(preferences.get("note_document_id") or ""),
                "score": round(best_score, 3),
                "question": _snippet(exchange["text"], terms) if exchange["text"] else "",
                "answer": _snippet(exchange["reply"], terms) if exchange["reply"] else "",
                "excerpt": _snippet(
                    (exchange["text"] + "\n" + exchange["reply"]).strip()
                    or title,
                    terms,
                ),
            }
        )
    hits.sort(key=lambda item: (item["score"], item["updated_at"]), reverse=True)
    return hits[:limit]


async def search_async(
    settings: Settings,
    query: str,
    *,
    limit: int = 5,
    page_id: str = "",
) -> list[dict[str, Any]]:
    """Search with embeddings when configured, lexical always.

    Lexical results are the fallback and the ranking seed; semantic similarity
    adds to the score of the matching sessions.
    """
    needle = (query or "").strip()
    if not needle:
        return []
    lexical = search(settings, needle, limit=50, page_id=page_id)
    if not lexical:
        return []
    try:
        cache = _load_cache(settings)
        items = []
        for hit in lexical:
            text = " ".join(part for part in (hit["question"], hit["answer"]) if part)
            items.append(
                {
                    "key": hit["session_id"],
                    "text": text[:2000] or hit["title"],
                    "hash": hashlib.sha256((text or hit["title"]).encode()).hexdigest(),
                }
            )
        semantic = await _semantic_scores(settings, cache, items, needle)
        if semantic:
            _save_cache(settings, cache)
            for hit in lexical:
                hit["score"] = round(hit["score"] + 3.0 * semantic.get(hit["session_id"], 0.0), 3)
            lexical.sort(key=lambda item: (item["score"], item["updated_at"]), reverse=True)
    except Exception:  # noqa: BLE001 - search must never fail the turn
        pass
    return lexical[: max(1, min(int(limit or 5), 20))]


def render(results: list[dict[str, Any]]) -> str:
    """Compact result blocks for a tool result."""
    if not results:
        return "No matching earlier conversations."
    lines: list[str] = []
    for hit in results:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(hit["updated_at"] or 0))
        lines.append(f'[{stamp}] {hit["title"]} (session {hit["session_id"][:12]})')
        if hit.get("question"):
            lines.append(f"  learner: {hit['question'][:500]}")
        if hit.get("answer"):
            lines.append(f"  tutor: {hit['answer'][:900]}")
        if not hit.get("question") and not hit.get("answer"):
            lines.append(f"  {hit['excerpt'][:600]}")
        lines.append("")
    return "\n".join(lines).strip()


__all__ = ["render", "search", "search_async"]
