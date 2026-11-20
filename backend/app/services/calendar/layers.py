"""Read-only projections of the selected workspace onto calendar days.

Spaced-repetition reviews are not implemented in this backend, so that list
is empty. Scheduled reminders/automations come from the cron service, and
*footprints* — the days the learner actually studied — are derived from
session transcripts.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Literal, cast

from ...config import get_settings
from ...store.session_store import SessionStore
from .models import AutomationRun, CalendarLayers, FootprintDay, FootprintSession
from .recurrence import format_instant, zone

MAX_SESSIONS_PER_DAY = 20
MAX_AUTOMATIONS = 1000


def owner_key() -> str:
    """One stable owner for locally scheduled jobs (single-user deployment)."""
    return "chat:local-admin"


def _automations(workspace_id: str, lo: float, hi: float) -> list[AutomationRun]:
    """Expand this workspace's enabled jobs into the days they will run on."""
    try:
        from ..cron import compute_next_run, get_cron_service

        service = get_cron_service()
    except Exception:  # a broken cron store must not break the calendar
        return []

    runs: list[AutomationRun] = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    lower = max(int(lo * 1000), now_ms)
    for job in service.list_jobs(owner_key()):
        if job.owner.workspace_id != workspace_id or not job.enabled:
            continue
        at = job.state.next_run_at_ms
        if at is None:
            continue
        if at < lower:
            if job.schedule.kind == "every" and job.schedule.every_seconds:
                step = job.schedule.every_seconds * 1000
                at += max(0, math.ceil((lower - at) / step)) * step
            else:
                at = compute_next_run(job.schedule, lower - 1)
        for _ in range(MAX_AUTOMATIONS):
            if at is None or at >= hi * 1000:
                break
            runs.append(
                AutomationRun(
                    id=f"{job.id}:{at}",
                    job_id=job.id,
                    name=job.name,
                    message=job.message,
                    at=format_instant(datetime.fromtimestamp(at / 1000, timezone.utc)),
                    kind=cast(Literal["at", "every", "cron"], job.schedule.kind),
                    expr=job.schedule.expr,
                    every_seconds=job.schedule.every_seconds,
                    session_id=job.owner.session_id or None,
                    enabled=job.enabled,
                )
            )
            at = compute_next_run(job.schedule, at)
    return runs


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
        automations=_automations(workspace_id, lo, hi),
        footprints=sorted(footprints.values(), key=lambda item: item.date),
    )
