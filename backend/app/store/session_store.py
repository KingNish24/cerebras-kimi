"""Session persistence for the vtx-powered backend.

Two layers live in one JSON document per session (`data/sessions/<id>.json`):

* ``vtx_items`` — the raw input-item dicts the vtx ``Session`` protocol
  consumes. ``VtxSessionAdapter`` implements that protocol so
  ``Runner.run_streamed(..., session=...)`` reads/writes history here.
* ``messages`` / ``turns`` — the UI-facing view: chat messages with their
  streamed events, and per-turn event logs used for WS replay after
  reconnects. This mirrors the shape ``web/lib/session-api.ts`` expects.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import Settings


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


EMPTY_SESSION: dict[str, Any] = {
    "id": "",
    "title": "",
    "created_at": 0.0,
    "updated_at": 0.0,
    "status": "idle",
    "preferences": {},
    "messages": [],
    "turns": {},
    "vtx_items": [],
    "next_message_id": 1,
    "last_input": None,
}


class SessionStore:
    """Filesystem-backed session index. One JSON file per session."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._settings.sessions_dir.mkdir(parents=True, exist_ok=True)

    # -- path helpers -----------------------------------------------------

    def _path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self._settings.sessions_dir / f"{safe}.json"

    # -- CRUD -------------------------------------------------------------

    def create(self, preferences: dict[str, Any] | None = None) -> dict[str, Any]:
        session = dict(EMPTY_SESSION)
        session["id"] = _new_id("sess")
        session["created_at"] = session["updated_at"] = _now()
        session["preferences"] = dict(preferences or {})
        self._write(session)
        return session

    def get(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = _now()
        self._write(session)

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        sessions = [s for s in (self.get(p.stem) for p in self._settings.sessions_dir.glob("*.json")) if s]
        sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return sessions[offset : offset + limit]

    def search(self, query: str, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        q = query.strip().lower()
        if not q:
            return [], 0
        hits: list[dict[str, Any]] = []
        for path in self._settings.sessions_dir.glob("*.json"):
            session = self.get(path.stem)
            if not session:
                continue
            excerpt = ""
            matched = False
            for message in session.get("messages", []):
                content = str(message.get("content", ""))
                idx = content.lower().find(q)
                if idx >= 0:
                    matched = True
                    start = max(0, idx - 40)
                    excerpt = content[start : idx + 120].replace("\n", " ")
                    break
            if not matched and q in str(session.get("title", "")).lower():
                matched = True
            if matched:
                hits.append({**session, "_match_excerpt": excerpt})
        hits.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return hits[offset : offset + limit], len(hits)

    # -- turn / message helpers -------------------------------------------

    def append_user_message(
        self,
        session: dict[str, Any],
        content: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": session["next_message_id"],
            "session_id": session["id"],
            "role": "user",
            "content": content,
            "capability": None,
            "events": [],
            "attachments": list(attachments or []),
            "metadata": dict(metadata or {}),
            "created_at": _now(),
            "parent_message_id": None,
        }
        session["next_message_id"] += 1
        session["messages"].append(message)
        if not session.get("title"):
            session["title"] = _derive_title(content)
        return message

    def append_assistant_message(
        self,
        session: dict[str, Any],
        content: str,
        *,
        events: list[dict[str, Any]],
        turn_id: str,
        capability: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": session["next_message_id"],
            "session_id": session["id"],
            "role": "assistant",
            "content": content,
            "capability": capability,
            "events": events,
            "attachments": [],
            "metadata": {"turn_id": turn_id, **(metadata or {})},
            "created_at": _now(),
            "parent_message_id": None,
        }
        session["next_message_id"] += 1
        session["messages"].append(message)
        return message

    def record_turn_event(
        self, session: dict[str, Any], turn_id: str, event: dict[str, Any]
    ) -> None:
        turn = session["turns"].setdefault(
            turn_id,
            {"events": [], "status": "running", "last_seq": 0, "created_at": _now()},
        )
        turn["events"].append(event)
        turn["last_seq"] = event.get("seq", turn["last_seq"])

    def finish_turn(
        self, session: dict[str, Any], turn_id: str, status: str
    ) -> None:
        turn = session["turns"].get(turn_id)
        if turn is not None:
            turn["status"] = status
            turn["finished_at"] = _now()
        session["status"] = "idle" if status == "completed" else status

    # -- internals ----------------------------------------------------------

    def _write(self, session: dict[str, Any]) -> None:
        path = self._path(session["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def _derive_title(content: str) -> str:
    text = " ".join(content.split())
    return text[:60] + ("…" if len(text) > 60 else "")


class VtxSessionAdapter:
    """vtx ``Session`` protocol over a session document's ``vtx_items``.

    The runner calls these methods around every turn; each mutation is
    persisted immediately so a crash mid-turn never loses committed history.
    """

    def __init__(self, store: SessionStore, session: dict[str, Any]) -> None:
        self._store = store
        self._session = session
        self.session_id = session["id"]

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = self._session.get("vtx_items", [])
        if limit is not None:
            items = items[-limit:]
        return [dict(item) for item in items]

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        self._session.setdefault("vtx_items", []).extend(dict(i) for i in items)
        self._store.save(self._session)

    async def pop_item(self) -> dict[str, Any] | None:
        items = self._session.get("vtx_items", [])
        if not items:
            return None
        item = items.pop()
        self._store.save(self._session)
        return item

    async def clear_session(self) -> None:
        self._session["vtx_items"] = []
        self._store.save(self._session)


def vtx_jsonl_session(store: "SessionStore", session: dict[str, Any], cwd: str) -> Any:
    """Return a vtx SDK session (JSONLSession) backed by ~/.vtx/sessions,
    so DeepTutor chats are the same sessions the vtx CLI/TUI resumes.

    The DeepTutor session record keeps ``vtx_session_file`` pointing at the
    JSONL file; content/history lives there, not in ``vtx_items``.
    """
    from pathlib import Path

    from vtx.agent.session import Session as VtxSession
    from vtx.agent.sdk.sessions import JSONLSession

    path = session.get("vtx_session_file")
    if path and Path(path).exists():
        js = JSONLSession()
        js._session = VtxSession.load(path)
        return js

    v = VtxSession.create(cwd=cwd, persist=True)
    session["vtx_session_file"] = str(v.session_file)
    store.save(session)
    js = JSONLSession()
    js._session = v
    return js
