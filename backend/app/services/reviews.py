"""Spaced repetition for flashcards (the question bank), feeding the
calendar's Reviews layer.

Every ``question_bank.json`` entry with no schedule yet gets a first review
one day after it was created. Correct answers advance a Leitner box with
growing intervals (1, 3, 7, 14, 30, 60 days); "again" resets to box 1.
State lives in ``data/reviews.json`` keyed by a hash of the question text, so
edits to the question simply start a new card.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from ..config import get_settings

# Days between reviews per box, 1-indexed.
INTERVALS_DAYS = (1, 3, 7, 14, 30, 60)
MAX_BOX = len(INTERVALS_DAYS)


def _bank_path() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve()
    return root / "question_bank.json"


def _state_path() -> Path:
    return get_settings().data_dir / "reviews.json"


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def key_for(question: str) -> str:
    return hashlib.sha1((question or "").strip().encode("utf-8")).hexdigest()[:16]


def _card(*, created: float, due_at: float, box: int = 1, last_at: float | None = None) -> dict[str, Any]:
    return {
        "created": created,
        "due_at": due_at,
        "box": box,
        "last_at": last_at,
    }


def sync() -> dict[str, Any]:
    """Register question-bank entries that are not scheduled yet."""
    try:
        bank = json.loads(_bank_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        bank = []
    state = _load_state()
    now = time.time()
    changed = False
    for entry in bank if isinstance(bank, list) else []:
        question = str(entry.get("q") or "").strip()
        if not question:
            continue
        key = key_for(question)
        if key in state:
            continue
        created = float(entry.get("created") or now)
        state[key] = _card(created=created, due_at=created + 86400, box=1)
        changed = True
    # Cards whose question disappeared are dropped (the bank is the source of
    # truth for content; state only tracks scheduling).
    live = {key_for(str(e.get("q") or "").strip()) for e in bank if str(e.get("q") or "").strip()}
    for key in [k for k in state if k not in live]:
        del state[key]
        changed = True
    if changed:
        _save_state(state)
    return state


def due_between(start_ts: float, end_ts: float) -> list[dict[str, Any]]:
    """Cards due in ``[start_ts, end_ts)``, with their question text."""
    state = sync()
    try:
        bank = json.loads(_bank_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        bank = []
    question_by_key = {key_for(str(e.get("q") or "")): e for e in bank if str(e.get("q") or "")}
    out: list[dict[str, Any]] = []
    for key, card in state.items():
        due = float(card.get("due_at") or 0)
        if start_ts <= due < end_ts:
            entry = question_by_key.get(key)
            out.append(
                {
                    "key": key,
                    "question": str(entry.get("q") or "") if entry else "",
                    "answer": str(entry.get("a") or "") if entry else "",
                    "topic": str(entry.get("topic") or "") if entry else "",
                    "box": int(card.get("box") or 1),
                    "due_at": due,
                }
            )
    return sorted(out, key=lambda item: item["due_at"])


def grade(key: str, correct: bool) -> dict[str, Any]:
    """Record one review; returns the card's new state."""
    card = sync().get(key)
    if card is None:
        raise KeyError(key)
    now = time.time()
    if correct:
        card["box"] = min(int(card.get("box") or 1) + 1, MAX_BOX)
        card["due_at"] = now + INTERVALS_DAYS[card["box"] - 1] * 86400
    else:
        card["box"] = 1
        card["due_at"] = now + INTERVALS_DAYS[0] * 86400
    card["last_at"] = now
    state = _load_state()
    state[key] = card
    _save_state(state)
    return card


def summary() -> dict[str, Any]:
    """Counts by box and how many are due now, for tool/REST consumers."""
    state = sync()
    now = time.time()
    boxes = {str(i): 0 for i in range(1, MAX_BOX + 1)}
    due_now = 0
    for card in state.values():
        box = min(max(int(card.get("box") or 1), 1), MAX_BOX)
        boxes[str(box)] = boxes.get(str(box), 0) + 1
        if float(card.get("due_at") or 0) <= now:
            due_now += 1
    return {"cards": len(state), "boxes": boxes, "due_now": due_now}


__all__ = ["INTERVALS_DAYS", "MAX_BOX", "due_between", "grade", "key_for", "summary", "sync"]