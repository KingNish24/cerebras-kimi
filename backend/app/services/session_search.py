"""Search across chat sessions, with optional embedding retrieval.

Dito can refer back to anything the learner and the tutor discussed, not just
the page it is currently beside. Session text lives in the WAL-mode session
database and is indexed by its FTS5 table, so a search never has to read the
whole history.

Retrieval is lexical by default (FTS5 ranking) so it works with no embedding
provider configured. When an embedding model is configured under
Settings → Embedding, message embeddings are cached in the session database and
the query is embedded as well; the two scores are combined.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any

import httpx

from .. import provider_config
from ..config import Settings
from ..store.session_store import SessionStore

_EMBED_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _store(settings: Settings) -> SessionStore:
    return SessionStore(settings)


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
            response = await client.post(
                f"{target['base_url']}/embeddings", headers=headers, json=body
            )
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
        item
        for item in items
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
    """Synchronous FTS5 search; ``page_id`` limits to one notebook's threads."""
    return _store(settings).search_hits(query, limit=limit, page_id=page_id)


async def search_async(
    settings: Settings,
    query: str,
    *,
    limit: int = 5,
    page_id: str = "",
) -> list[dict[str, Any]]:
    """Search with embeddings when configured, FTS5 lexical always.

    Lexical results are the fallback and the ranking seed; semantic similarity
    adds to the score of the matching sessions.
    """
    needle = (query or "").strip()
    if not needle:
        return []
    lexical = search(settings, needle, limit=max(limit, 20), page_id=page_id)
    if not lexical:
        return []
    try:
        cache = _store(settings).load_embeddings()
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
            _store(settings).save_embeddings(cache)
            for hit in lexical:
                hit["score"] = round(
                    hit["score"] + 3.0 * semantic.get(hit["session_id"], 0.0), 3
                )
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
