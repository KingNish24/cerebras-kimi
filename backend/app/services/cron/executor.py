"""Run a due cron job for its owner (a DeepTutor chat session).

The scheduler is background-only: no request is in flight, so the job runs a
normal turn through :class:`AgentRuntime` and the exchange lands in the
session it was scheduled from. The learner sees the reminder in their history
(and in the calendar's Automations layer) even if no tab was open.
"""

from __future__ import annotations

import asyncio
import logging

from .reports import record as record_report
from .service import CronJob

log = logging.getLogger("deeptutor.cron")

TURN_TIMEOUT_SECONDS = 300
POLL_SECONDS = 0.25


class _RuntimeRegistry:
    """Holds the process-wide AgentRuntime for background jobs."""

    runtime = None

    @classmethod
    def set(cls, runtime) -> None:  # noqa: ANN001 - avoids importing runtime here
        cls.runtime = runtime


def set_runtime(runtime) -> None:  # noqa: ANN001
    _RuntimeRegistry.set(runtime)


def reminder_prompt(job: CronJob) -> str:
    return (
        "The scheduled time has arrived. Deliver this reminder to the user now, "
        "as a brief and natural message in their language. Speak directly to them; "
        "do not narrate scheduler status or mention internal job ids. Answer in "
        "plain conversational text — do not emit UI, Remotion or OpenUI code.\n\n"
        f"Reminder: {job.message}"
    )


async def execute_job(job: CronJob) -> tuple[str, str | None]:
    """Returns ``(status, error)`` — status is ok/error/skipped."""
    if job.owner.kind != "chat":
        return "skipped", f"unsupported owner kind {job.owner.kind!r}"

    runtime = _RuntimeRegistry.runtime
    if runtime is None:
        return "error", "agent runtime is not available"

    session_id = (job.owner.session_id or "").strip()
    if not session_id:
        # A plain reminder ("remind me to stretch"): nothing to ask the model,
        # so the report buffer is the delivery channel (toast + notification).
        record_report(
            job_id=job.id,
            name=job.name,
            text=job.message,
            status="ok",
        )
        return "ok", None
    if runtime._store.get(session_id) is None:  # noqa: SLF001 - store is the session truth
        return "skipped", "session no longer exists"

    prompt = reminder_prompt(job)
    before = len((runtime._store.get(session_id) or {}).get("messages") or [])  # noqa: SLF001
    handle, _session_event, error = await runtime.start_turn(
        {
            "content": prompt,
            "session_id": session_id,
            "capability": "chat",
            "workspace_id": job.owner.workspace_id,
        }
    )
    if handle is None:
        return "error", error or "could not start the turn"

    for _ in range(int(TURN_TIMEOUT_SECONDS / POLL_SECONDS)):
        if handle.status != "running":
            break
        await asyncio.sleep(POLL_SECONDS)
    else:
        return "error", "turn did not finish in time"

    # The assistant row is appended just before the handle flips status; read
    # again briefly so a reminder that ran to completion is not missed.
    for _ in range(20):
        session = runtime._store.get(session_id) or {}  # noqa: SLF001
        messages = (session.get("messages") or [])[before:]
        answer = next(
            (
                str(message.get("content") or "")
                for message in messages
                if message.get("role") == "assistant" and str(message.get("content") or "").strip()
            ),
            "",
        )
        if answer:
            record_report(job_id=job.id, name=job.name, text=answer, status="ok")
            return "ok", None
        if handle.status not in {"running", "completed", "done"}:
            break
        await asyncio.sleep(POLL_SECONDS)

    # A completed turn with no captured text still delivered whatever the
    # model said; anything else is a genuine failure worth surfacing.
    if handle.status in {"completed", "done"}:
        record_report(job_id=job.id, name=job.name, text=job.message, status="ok")
        return "ok", None
    return "error", f"turn finished with status {handle.status!r}"


__all__ = ["execute_job", "reminder_prompt", "set_runtime"]
