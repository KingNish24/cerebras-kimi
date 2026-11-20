"""A vtx Session whose persistence is DeepTutor's session record.

The agent talks to a normal ``vtx.agent.session.Session``, so nothing in the
vtx runtime needs to change. But instead of writing ``~/.vtx/sessions/<cwd>/*.jsonl``,
entries and the header are stored in the DeepTutor session row
(``data/deeptutor.sqlite3``) under ``vtx_header`` / ``vtx_entries``.

A ContextVar binds the DeepTutor store + record while the runtime creates its
session. Sessions created outside that context stay in memory (still no vtx
files) — that covers sub-agents and other incidental Session.create() calls.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from vtx.agent.session import (
    CompactionEntry,
    CustomMessageEntry,
    HarnessStateEntry,
    LeafEntry,
    MessageEntry,
    ModelChangeEntry,
    RuntimeCheckpointEntry,
    Session,
    SessionHeader,
    SessionInfoEntry,
    ThinkingLevelChangeEntry,
)

_DT_CONTEXT: ContextVar[tuple[Any, dict[str, Any]] | None] = ContextVar(
    "deeptutor_session_context", default=None
)


def bind(store: Any, record: dict[str, Any]):
    """Bind a DeepTutor session for the duration of a ContextManager block."""
    return _DT_CONTEXT.set((store, record))


def unbind(token) -> None:
    _DT_CONTEXT.reset(token)


def rebind(session: Any, store: Any, record: dict[str, Any]) -> None:
    """Point an already-created vtx Session at this turn's record.

    ConversationRuntime is cached per DeepTutor session, but ``start_turn``
    loads a fresh dict on every turn. Without this, vtx keeps writing its
    entries into the first turn's dict while the turn-end save persists the
    fresh one — so the model only ever sees the first turn's history.
    """
    if (
        getattr(session, "_dt_store", None) is store
        and getattr(session, "_dt_record", None) is record
    ):
        return
    session._dt_store = store
    session._dt_record = record


_ENTRY_TYPES = {
    "message": MessageEntry,
    "thinking_level_change": ThinkingLevelChangeEntry,
    "model_change": ModelChangeEntry,
    "compaction": CompactionEntry,
    "custom_message": CustomMessageEntry,
    "harness_state": HarnessStateEntry,
    "session_info": SessionInfoEntry,
    "leaf": LeafEntry,
    "runtime_checkpoint": RuntimeCheckpointEntry,
}


class DeepTutorSession(Session):
    """Session with DeepTutor-backed persistence and no JSONL on disk."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        context = _DT_CONTEXT.get()
        self._dt_store = context[0] if context else None
        self._dt_record = context[1] if context else None
        # Persistence is handled by the overrides below, not by vtx's file I/O.
        kwargs["persist"] = True
        super().__init__(*args, **kwargs)

    def _load_from_record(self) -> None:
        record = self._dt_record
        if not record:
            return
        header_data = record.get("vtx_header")
        if isinstance(header_data, dict):
            self._header = SessionHeader.model_validate(header_data)
        entries: list[Any] = []
        for raw in record.get("vtx_entries") or []:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                model = _ENTRY_TYPES.get(str(data.get("type")))
                if model is not None:
                    entries.append(model.model_validate(data))
            except Exception:  # noqa: BLE001
                continue
        self._entries = entries
        self._by_id = {entry.id: entry for entry in entries}
        self._leaf_id = None
        for entry in entries:
            self._leaf_id = entry.target_id if isinstance(entry, LeafEntry) else entry.id
        self._persisted_entries_count = len(entries)
        self._flushed = True

    def _write_record(self) -> None:
        if self._dt_store is None or self._dt_record is None:
            return
        self._dt_record["vtx_header"] = (
            self._header.model_dump(mode="json") if self._header else None
        )
        self._dt_record["vtx_entries"] = [
            entry.model_dump_json() for entry in self._entries
        ]
        self._dt_store.save(self._dt_record)

    # -- vtx persistence hooks ------------------------------------------------

    def _persist_entry(self, entry: Any) -> None:
        self._write_record()

    def _write_all(self) -> None:
        self._write_record()

    def ensure_persisted(self) -> None:
        self._write_record()
