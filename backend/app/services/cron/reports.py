"""Recent reminders that actually fired, for live in-app delivery.

A due job writes one entry here so an open tab can surface it without
reloading the conversation: the calendar runtime polls
``GET /api/cron/reports?since=`` and raises the same toast/notification it
uses for event reminders. The store is a small JSON ring buffer — the durable
copy of every reminder is the conversation it was appended to.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ...config import get_settings

MAX_REPORTS = 200


def _path() -> Path:
    return get_settings().data_dir / "cron" / "reports.json"


def _read() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def record(*, job_id: str, name: str, text: str, status: str) -> dict[str, Any]:
    """Append one delivered reminder and return its entry."""
    entry = {
        "id": f"{job_id}:{int(time.time() * 1000)}",
        "job_id": job_id,
        "name": name,
        "text": " ".join((text or "").split())[:1000],
        "status": status,
        "at": time.time(),
    }
    items = [*_read(), entry][-MAX_REPORTS:]
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return entry


def since(epoch_seconds: float) -> list[dict[str, Any]]:
    """Reports at or after ``epoch_seconds``, oldest first."""
    return [item for item in _read() if float(item.get("at") or 0) >= epoch_seconds]


__all__ = ["record", "since"]
