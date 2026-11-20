"""Read-only projections of the selected workspace onto calendar days.

DeepTutor's upstream calendar merges scheduled spaced-repetition reviews and
cron automations into the day grid. This backend has neither store yet, so
those lists are returned empty and only *footprints* — the days the learner
actually studied, derived from session transcripts — are projected.
"""

from __future__ import annotations

from datetime import datetime

from ...config import get_settings
from ...store.session_store import SessionStore
from .models import CalendarLayers, FootprintDay, FootprintSession
from .recurrence import zone

MAX_SESSIONS_PER_DAY = 20


def owner_key() -> str:
    """DeepTutor is single-user locally; one stable owner for cron-style jobs."""
    return "local"


def _session_store() -> SessionStore:
    return SessionStore(get_settings())


def read_layers(workspace_id: str, start: datetime, end: datetime, tzid: str) -> CalendarLayers:
    """Project this workspace's study activity onto days in ``tzid``.

    Args:
        workspace_id: Content workspace to project; ``""`` is General.
        start: Inclusive lower bound of the requested range (aware UTC).
        end: Exclusive upper bound of the requested range (aware UTC).
        tzid: Zone the day buckets are named in.

    Returns:
        Day buckets with the sessions that had a learner message in them.
    """
    tz = zone(tzid)
    lo, hi = start.timestamp(), end.timestamp()
    footprints: dict[str, FootprintDay] = {}

    try:
        sessions = _session_store().all()
    except Exception:  # a corrupt or missing session file must not break the calendar
        sessions = []

    for session in sessions:
        workspace = (session.get("preferences") or {}).get("workspace_id") or ""
        if workspace != workspace_id:
            continue
        title = str(session.get("title") or "")
        for message in session.get("messages") or []:
            if message.get("role") != "user":
                continue
            stamp = message.get("created_at")
            if not isinstance(stamp, (int, float)):
                continue
            if not lo <= stamp < hi:
                continue
            key = datetime.fromtimestamp(stamp, tz).date().isoformat()
            day = footprints.setdefault(key, FootprintDay(date=key))
            if all(entry.id != session["id"] for entry in day.sessions):
                if len(day.sessions) < MAX_SESSIONS_PER_DAY:
                    day.sessions.append(FootprintSession(id=session["id"], title=title))
                day.session_count += 1

    return CalendarLayers(
        workspace_id=workspace_id,
        reviews=[],
        automations=[],
        footprints=sorted(footprints.values(), key=lambda item: item.date),
    )
