"""REST surface for scheduled reminders and review cards.

Mounted at ``/api/cron`` and reused by the calendar page, the composer's
"Remind me" control, and the reminder runtime. Everything is scoped to the
request's content workspace (``dt_workspace``), matching the calendar.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..services.cron import CronOwner, CronSchedule, get_cron_service
from ..services.cron import reports as cron_reports

router = APIRouter(tags=["cron"])


def _workspace(request: Request) -> str:
    params = request.query_params
    value = params.get("dt_workspace")
    if value is None:
        value = params.get("workspace")
    return value or ""


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    name: str = ""
    at: str = ""
    every_seconds: int = 0
    cron_expr: str = ""
    tz: str = ""
    session_id: str = ""  # optional: without one the reminder is notify-only
    workspace_id: str = ""  # optional; the request scope wins when non-empty


def _build_schedule(payload: CreateJob) -> CronSchedule:
    chosen = [bool(payload.at.strip()), bool(payload.every_seconds), bool(payload.cron_expr.strip())]
    if sum(chosen) != 1:
        raise HTTPException(400, "Provide exactly one of: at, every_seconds or cron_expr.")
    if payload.at.strip():
        try:
            parsed = datetime.fromisoformat(payload.at.strip())
        except ValueError:
            raise HTTPException(400, "at must be ISO 8601, e.g. 2026-06-12T09:00") from None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return CronSchedule(kind="at", at_ms=int(parsed.timestamp() * 1000))
    if payload.every_seconds:
        return CronSchedule(kind="every", every_seconds=int(payload.every_seconds))
    return CronSchedule(
        kind="cron", expr=payload.cron_expr.strip(), tz=(payload.tz or "").strip() or None
    )


def _job_payload(job) -> dict[str, object]:  # noqa: ANN001 - CronJob
    return {
        "id": job.id,
        "name": job.name,
        "message": job.message,
        "kind": job.schedule.kind,
        "at_ms": job.schedule.at_ms,
        "every_seconds": job.schedule.every_seconds,
        "expr": job.schedule.expr,
        "tz": job.schedule.tz,
        "enabled": job.enabled,
        "session_id": job.owner.session_id or "",
        "workspace_id": job.owner.workspace_id or "",
        "next_run_at_ms": job.state.next_run_at_ms,
        "last_status": job.state.last_status,
        "last_error": job.state.last_error,
    }


@router.get("/api/cron/jobs")
def list_jobs(request: Request):
    from ..services.calendar.layers import owner_key

    service = get_cron_service()
    jobs = [
        job
        for job in service.list_jobs(owner_key())
        if job.owner.workspace_id == _workspace(request)
    ]
    return {"jobs": [_job_payload(job) for job in jobs]}


@router.post("/api/cron/jobs", status_code=201)
def create_job(payload: CreateJob, request: Request):
    try:
        schedule = _build_schedule(payload)
        job = get_cron_service().add_job(
            name=payload.name,
            message=payload.message,
            schedule=schedule,
            owner=CronOwner(
                kind="chat",
                session_id=payload.session_id.strip(),
                workspace_id=payload.workspace_id.strip() or _workspace(request),
            ),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"job": _job_payload(job), "ok": True}


@router.delete("/api/cron/jobs/{job_id}")
def cancel_job(job_id: str, request: Request):
    from ..services.calendar.layers import owner_key

    service = get_cron_service()
    job = service.get_job(job_id)
    if (
        not job
        or job.owner.key != owner_key()
        or job.owner.kind != "chat"
        or job.owner.workspace_id != _workspace(request)
    ):
        raise HTTPException(404, "Reminder not found.")
    return {"ok": service.cancel_job(job_id, owner_key=owner_key())}


@router.get("/api/cron/reports")
def recent_reports(
    since: float = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Reminders that fired, for live delivery to open tabs."""
    return {"reports": cron_reports.since(since)[-limit:]}


@router.get("/api/reviews/due")
def due_reviews(limit: int = Query(default=20, ge=1, le=100)):
    from ..services.reviews import due_between, summary

    now = datetime.now(timezone.utc).timestamp()
    return {"due": due_between(now, now + 86400 * 30)[:limit], "summary": summary()}


@router.post("/api/reviews/{key}/grade")
def grade_review(key: str, correct: bool = True):
    from ..services.reviews import grade

    try:
        card = grade(key, bool(correct))
    except KeyError:
        raise HTTPException(404, "Review card not found.") from None
    return {"ok": True, "card": card}