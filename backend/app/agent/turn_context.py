"""Per-turn context available to tools that must know who is asking.

Tools are plain functions, so the session and content workspace of the
running turn are published in context variables instead of being passed as
tool arguments (the model must never choose them). ``AgentRuntime.start_turn``
sets both before it creates the turn task, so ``asyncio.create_task`` copies
them into the turn's own context.
"""

from __future__ import annotations

from contextvars import ContextVar

current_session_id: ContextVar[str] = ContextVar("dt_session_id", default="")
current_workspace_id: ContextVar[str] = ContextVar("dt_workspace_id", default="")


def set_turn_context(session_id: str, workspace_id: str) -> None:
    current_session_id.set(session_id or "")
    current_workspace_id.set(workspace_id or "")


def turn_context() -> tuple[str, str]:
    """``(session_id, workspace_id)`` of the running turn; both may be empty."""
    return current_session_id.get(), current_workspace_id.get()


__all__ = ["current_session_id", "current_workspace_id", "set_turn_context", "turn_context"]
