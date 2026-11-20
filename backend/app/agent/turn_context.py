"""Per-turn context available to tools that must know who is asking.

Tools are plain functions, so the session, workspace and bound notebook page
of the running turn are published in context variables instead of being passed
as tool arguments (the model must never choose them). ``AgentRuntime.start_turn``
sets them before it creates the turn task, so ``asyncio.create_task`` copies
them into the turn's own context.
"""

from __future__ import annotations

from contextvars import ContextVar

current_session_id: ContextVar[str] = ContextVar("dt_session_id", default="")
current_workspace_id: ContextVar[str] = ContextVar("dt_workspace_id", default="")
current_note_document_id: ContextVar[str] = ContextVar("dt_note_document_id", default="")
current_note_block_id: ContextVar[str] = ContextVar("dt_note_block_id", default="")
current_note_generation_kind: ContextVar[str] = ContextVar("dt_note_generation_kind", default="")


def set_turn_context(session_id: str, workspace_id: str) -> None:
    current_session_id.set(session_id or "")
    current_workspace_id.set(workspace_id or "")


def set_note_context(
    note_document_id: str, block_id: str = "", generation_kind: str = ""
) -> None:
    """Bind the turn to a notebook page (and optionally one block on it)."""
    current_note_document_id.set(note_document_id or "")
    current_note_block_id.set(block_id or "")
    current_note_generation_kind.set(generation_kind or "")


def turn_context() -> tuple[str, str]:
    """``(session_id, workspace_id)`` of the running turn; both may be empty."""
    return current_session_id.get(), current_workspace_id.get()


def note_context() -> tuple[str, str, str]:
    """``(page_id, block_id, generation_kind)``; empty when not a note thread."""
    return (
        current_note_document_id.get(),
        current_note_block_id.get(),
        current_note_generation_kind.get(),
    )


__all__ = [
    "current_session_id",
    "current_workspace_id",
    "current_note_document_id",
    "current_note_block_id",
    "current_note_generation_kind",
    "set_turn_context",
    "set_note_context",
    "turn_context",
    "note_context",
]
