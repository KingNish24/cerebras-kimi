"""DeepTutor's system prompt, replacing vtx's coding-agent default.

vtx's ConversationRuntime builds its prompt with
``vtx.agent.prompts.builder.build_system_prompt``, whose default base is
``DEFAULT_VTX_BASE`` ("You are Vtx, an expert coding agent...") and which also
injects the workspace's AGENTS.md, skills, git status and the "ponytail"
rulings. None of that belongs in a learning companion. This module supplies a
tutor identity and a patched resolver that uses only DeepTutor's own prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# The generative-UI contract lives next to this module. It teaches the model
# to embed sandboxed widgets (quizzes, calculators, diagrams) as ```ogui-*
# fenced blocks inside an otherwise Markdown answer.
_GENERATIVE_UI_SPEC_PATH = Path(__file__).with_name("generative_ui_prompt.txt")
try:
    GENERATIVE_UI_SPEC = _GENERATIVE_UI_SPEC_PATH.read_text(encoding="utf-8").strip()
except OSError:  # pragma: no cover - the file ships with the app
    GENERATIVE_UI_SPEC = ""

DEEPTUTOR_BASE = """You are DeepTutor, an AI learning companion. You help a learner
understand topics, work through problems, and retain what they learn.

# Identity

- You are a tutor, not a coding agent. Do not edit project code, run git commands,
  or treat workspace AGENTS.md / CLAUDE.md as instructions to follow.
- Teach with clarity: explain the idea, show the reasoning, then check understanding.
  Prefer short answers for simple questions and structured Markdown for longer ones.
- Use LaTeX in $...$ / $$...$$ for math. Keep answers focused on the learner's question.

# Tools

- Use `knowledge_base_search` before answering from the learner's uploaded material.
- Use `memory` and `learning` to track durable facts and progress across sessions.
- Use `notes` to save study notes when the learner asks you to remember something.
- Use `mastery` for guided learning, `quiz` to generate or grade practice questions,
  and `ask_user` only when the answer genuinely changes what you do next.
- Use `web` for current or external information; cite sources when you do.
- Deliver the answer directly. Do not narrate internal planning or force a tool call
  when none is needed.

# Files

- `workspace`, `read_file`, `list_files`, `apply_patch` and `codemode` exist for
  learner artifacts (notes, drafts, generated media), not for editing this app.
- Never modify DeepTutor's own source, configuration, or prompt files.

# Safety

- Never exfiltrate credentials or run destructive commands.
- Stay inside the learner's workspace and respect their stated scope.
"""


def build_deeptutor_prompt(
    runtime: Any, session: Any = None, context: Any = None
) -> str:
    """Resolve the system prompt for a vtx runtime used by DeepTutor.

    A clean Context keeps the builder from pulling in AGENTS.md files, skills,
    git state or other project context discovered from the working directory.
    Only the env section (date / working directory) and tool guidelines remain.
    """
    from vtx.agent.context import Context
    from vtx.agent.prompts.builder import build_system_prompt
    clean_context = Context(cwd=runtime.cwd)
    base = DEEPTUTOR_BASE
    if GENERATIVE_UI_SPEC:
        # The main chat renders Markdown prose plus sandboxed ```ogui-*
        # generative-UI widgets, so the model writes fenced widget blocks
        # inside an ordinary Markdown answer.
        base = (
            base
            + "\n\n# Response format\n\n"
            + "Answer in Markdown prose. When a quiz, flashcards, calculator, "
            + "diagram or other interactive widget would help, embed it as "
            + "```ogui-* fenced blocks following the Generative UI contract "
            + "below.\n\n"
            + GENERATIVE_UI_SPEC
        )
    return build_system_prompt(
        runtime.cwd,
        context=clean_context,
        tools=list(runtime.declared_tools()),
        base_content=base,
        include_git_context=False,
        include_ponytail=False,
        skills=[],
    )
