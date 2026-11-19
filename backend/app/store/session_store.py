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

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any, List

from ..config import Settings


# Sentinel for ``append_user_message``: the wire may omit ``parent_message_id``
# entirely, which means "continue from the latest persisted row" rather than
# the explicit root edit signalled by ``null``.
AUTO_PARENT_MESSAGE = object()


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
        session = copy.deepcopy(EMPTY_SESSION)
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
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        self._repair_legacy_message_tree(session)
        return session

    @staticmethod
    def _resolve_parent_message_id(
        session: dict[str, Any], parent_message_id: Any
    ) -> int | None:
        messages = session.get("messages") or []
        latest_id = messages[-1].get("id") if messages else None
        if parent_message_id is AUTO_PARENT_MESSAGE:
            return latest_id
        if parent_message_id is None:
            return None
        if (
            isinstance(parent_message_id, int)
            and not isinstance(parent_message_id, bool)
            and parent_message_id > 0
            and any(m.get("id") == parent_message_id for m in messages)
        ):
            return parent_message_id
        return latest_id

    def _repair_legacy_message_tree(self, session: dict[str, Any]) -> bool:
        """Rebuild parent links for sessions written before branching existed.

        Legacy rows all carry ``parent_message_id: null``, which the web client
        interprets as competing root branches and collapses to a single visible
        message. A leading run of null-parent rows is therefore a flat
        transcript: chain it in storage order. Later null rows are left alone —
        they can be legitimate root siblings created by editing the first
        message.
        """
        messages = session.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            return False
        if not all(isinstance(message, dict) for message in messages):
            return False

        changed = False
        old_ids = [message.get("id") for message in messages]
        id_values = [value for value in old_ids if isinstance(value, int) and not isinstance(value, bool)]
        needs_reid = (
            len(id_values) != len(old_ids)
            or len(set(id_values)) != len(id_values)
            or any(value <= 0 for value in id_values)
        )
        if needs_reid:
            old_to_latest: dict[Any, int] = {}
            for index, old_id in enumerate(old_ids):
                if old_id is not None:
                    old_to_latest[old_id] = index + 1
            for index, message in enumerate(messages):
                message["id"] = index + 1
            for index, message in enumerate(messages):
                parent = message.get("parent_message_id")
                if parent is None:
                    continue
                new_parent = None
                for candidate in range(index - 1, -1, -1):
                    if old_ids[candidate] == parent:
                        new_parent = candidate + 1
                        break
                message["parent_message_id"] = new_parent
            preferences = session.get("preferences")
            selections = preferences.get("selected_branches") if isinstance(preferences, dict) else None
            if isinstance(selections, dict):
                remapped: dict[str, int] = {}
                for parent_key, child_id in selections.items():
                    if parent_key == "null":
                        new_parent_key = "null"
                    else:
                        try:
                            new_parent_key = str(old_to_latest.get(int(parent_key), parent_key))
                        except (TypeError, ValueError):
                            continue
                    new_child = old_to_latest.get(child_id)
                    if new_child is not None:
                        remapped[new_parent_key] = new_child
                preferences["selected_branches"] = remapped
            session["next_message_id"] = len(messages) + 1
            changed = True
        else:
            max_id = max(id_values)
            if int(session.get("next_message_id") or 0) <= max_id:
                session["next_message_id"] = max_id + 1
                changed = True

        first_linked = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("parent_message_id") is not None
            ),
            len(messages),
        )
        legacy_run = first_linked if first_linked < len(messages) else len(messages)
        if legacy_run < 2:
            return changed

        # Assistant rows always continue a user turn, so a null-parent
        # assistant is unambiguous legacy-flat evidence. A prefix containing
        # only user rows may instead be deliberate root-edit siblings; only
        # collapse it when the entire session is flat.
        has_flat_assistant = any(
            messages[index].get("role") == "assistant"
            and messages[index].get("parent_message_id") is None
            for index in range(legacy_run)
        )
        if not has_flat_assistant and first_linked != len(messages):
            return changed

        for index in range(1, legacy_run):
            expected_parent = messages[index - 1].get("id")
            if messages[index].get("parent_message_id") != expected_parent:
                messages[index]["parent_message_id"] = expected_parent
                changed = True
        return changed

    def save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = _now()
        self._write(session)

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def all(self) -> List[dict[str, Any]]:
        """Every stored session, newest first.

        Callers that merge other sources (native vtx sessions) into the index
        need the unpaginated list so offset/limit can be applied once to the
        combined result instead of per-source.
        """
        sessions = [s for s in (self.get(p.stem) for p in self._settings.sessions_dir.glob("*.json")) if s]
        sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return sessions

    def list(self, limit: int = 50, offset: int = 0) -> List[dict[str, Any]]:
        return self.all()[offset : offset + limit]

    def search(self, query: str, limit: int = 50, offset: int = 0) -> tuple[List[dict[str, Any]], int]:
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
        attachments: List[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        parent_message_id: Any = AUTO_PARENT_MESSAGE,
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
            "parent_message_id": self._resolve_parent_message_id(
                session, parent_message_id
            ),
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
        events: List[dict[str, Any]],
        turn_id: str,
        capability: str | None = None,
        metadata: dict[str, Any] | None = None,
        parent_message_id: Any = AUTO_PARENT_MESSAGE,
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
            "parent_message_id": self._resolve_parent_message_id(
                session, parent_message_id
            ),
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
        # The data directory can be removed while the server is running (a
        # reset, a container remount, a user cleanup). Never assume it is
        # still there — recreate it before every write so a single missing
        # directory cannot break every turn.
        path = self._path(session["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
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


