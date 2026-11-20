"""Conversation memory for Dito, the notebook companion.

Dito is a temporary pairing agent: its conversations live in the normal chat
sessions, but the notebook keeps a small memory of what was discussed at each
page so a later Dito thread can recall the earlier ones. Memory is stored per
page under ``<data_dir>/dito/memory/<page_id>.json`` and is written after each
completed turn by :func:`app.agent.dito.remember_turn`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from ..config import get_settings
from .documents.storage import _atomic_write_json, _read_json

_MAX_TURNS = 200
_MAX_SESSIONS = 100
_MAX_USER = 4000
_MAX_ASSISTANT = 8000
_LOCK = threading.Lock()


def _root() -> Path:
    root = get_settings().data_dir / "dito" / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(page_id: str) -> Path:
    safe = "".join(ch for ch in page_id if ch.isalnum() or ch in "-_") or "unknown"
    return _root() / f"{safe}.json"


def _load(page_id: str) -> dict[str, Any]:
    data = _read_json(_path(page_id))
    if not isinstance(data, dict):
        return {"page_id": page_id, "sessions": {}}
    data.setdefault("sessions", {})
    return data


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def remember_turn(
    *,
    page_id: str,
    session_id: str,
    block_id: str = "",
    user_text: str = "",
    assistant_text: str = "",
    session_title: str = "",
    page_title: str = "",
) -> None:
    """Append one completed exchange to the page's Dito memory."""
    if not page_id or not session_id:
        return
    if not (str(user_text).strip() or str(assistant_text).strip()):
        return
    now = time.time()
    path = _path(page_id)
    with _LOCK:
        data = _load(page_id)
        data["page_id"] = page_id
        if page_title:
            data["page_title"] = page_title
        data["updated_at"] = now
        sessions = data["sessions"]
        session = sessions.get(session_id)
        if not isinstance(session, dict):
            session = {
                "session_id": session_id,
                "created_at": now,
                "turns": [],
            }
            sessions[session_id] = session
        session["updated_at"] = now
        if block_id:
            session["block_id"] = block_id
        if session_title:
            session["title"] = session_title
        turns = session.setdefault("turns", [])
        turns.append(
            {
                "at": now,
                "user": _clip(user_text, _MAX_USER),
                "assistant": _clip(assistant_text, _MAX_ASSISTANT),
            }
        )
        session["turns"] = turns[-_MAX_TURNS:]
        if len(sessions) > _MAX_SESSIONS:
            keep = sorted(
                sessions.values(),
                key=lambda item: float(item.get("updated_at") or 0),
                reverse=True,
            )[:_MAX_SESSIONS]
            data["sessions"] = {item["session_id"]: item for item in keep}
        _atomic_write_json(path, data)


def _page_files() -> list[Path]:
    return sorted(_root().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def conversations(page_id: str = "", query: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """Recent conversations matching ``query``, newest first.

    With ``page_id`` only that page is searched; otherwise every page's memory
    is scanned. Empty ``query`` returns the latest conversations.
    """
    needle = (query or "").strip().lower()
    limit = max(1, min(int(limit or 5), 25))
    results: list[dict[str, Any]] = []
    paths = [_path(page_id)] if page_id else _page_files()
    for path in paths:
        if not path.exists():
            continue
        page = _read_json(path)
        if not isinstance(page, dict):
            continue
        page_title = str(page.get("page_title") or "")
        sessions = page.get("sessions") or {}
        if not isinstance(sessions, dict):
            continue
        for session in sessions.values():
            if not isinstance(session, dict):
                continue
            turns = [t for t in session.get("turns") or [] if isinstance(t, dict)]
            title = str(session.get("title") or "")
            haystack = " ".join(
                [page_title, title]
                + [str(t.get("user") or "") + " " + str(t.get("assistant") or "") for t in turns]
            ).lower()
            if needle and needle not in haystack:
                continue
            results.append(
                {
                    "page_id": str(page.get("page_id") or page_id),
                    "page_title": page_title,
                    "session_id": str(session.get("session_id") or ""),
                    "title": title,
                    "block_id": str(session.get("block_id") or ""),
                    "updated_at": float(session.get("updated_at") or 0),
                    "turns": turns[-3:],
                }
            )
    results.sort(key=lambda item: item["updated_at"], reverse=True)
    return results[:limit]


def render(results: list[dict[str, Any]]) -> str:
    """Compact transcript blocks for a tool result."""
    if not results:
        return "No earlier Dito conversations found."
    lines: list[str] = []
    for item in results:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["updated_at"] or 0))
        where = item.get("page_title") or item["page_id"]
        title = f' — "{item["title"]}"' if item.get("title") else ""
        lines.append(f"[{stamp}] {where}{title} (session {item['session_id'][:12]})")
        for turn in item.get("turns") or []:
            user = str(turn.get("user") or "").strip().replace("\n", " ")
            reply = str(turn.get("assistant") or "").strip().replace("\n", " ")
            if user:
                lines.append(f"  learner: {user[:500]}")
            if reply:
                lines.append(f"  Dito: {reply[:900]}")
        lines.append("")
    return "\n".join(lines).strip()


def forget_page(page_id: str) -> bool:
    """Drop one page's Dito memory (used when a page is deleted)."""
    path = _path(page_id)
    if not path.exists():
        return False
    path.unlink()
    return True


__all__ = ["conversations", "forget_page", "remember_turn", "render"]
