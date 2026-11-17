"""Ports of DeepTutor's built-in tools (``reff/deeptutor/tools``) onto vtx ``@tool``.

Only self-contained tools are ported — anything needing DeepTutor services
(KB/RAG indexing, mastery engine, partners, media generation, cron, vision)
is intentionally absent. LLM-backed tools (``reason``, ``brainstorm``) run a
tool-less sub-agent through the same vtx provider resolution.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from .toolkit import tool

from ..config import Settings

_HEADERS = {"User-Agent": "DeepTutor/0.1 (+https://deeptutor.info)"}


# paper_search — arXiv
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="📄")
async def paper_search(
    query: str, max_results: int = 5, sort_by: str = "relevance"
) -> str:
    """Search arXiv for research papers.

    :param query: Search keywords (arXiv query syntax supported).
    :param max_results: Number of papers to return (max 20).
    :param sort_by: "relevance" or "date".
    """
    import xml.etree.ElementTree as ET

    query = (query or "").strip()
    if not query:
        return "Empty query."
    max_results = max(1, min(int(max_results), 20))
    sort = "submittedDate" if sort_by == "date" else "relevance"
    api = (
        "https://export.arxiv.org/api/query?search_query="
        + httpx.QueryParams({"search_query": f"all:{query}", "start": 0,
                             "max_results": max_results, "sortBy": sort,
                             "sortOrder": "descending"})
    )
    # httpx.QueryParams returns str-like; build manually to keep it simple
    from urllib.parse import urlencode

    api = "https://export.arxiv.org/api/query?" + urlencode(
        {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": sort,
            "sortOrder": "descending",
        }
    )
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            resp = await client.get(api)
            resp.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.text)
        out = []
        for entry in root.findall("a:entry", ns):
            title = " ".join((entry.findtext("a:title", "", ns) or "").split())
            summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())
            link = ""
            for ln in entry.findall("a:link", ns):
                if ln.get("title") == "pdf" or ln.get("rel") == "related":
                    link = ln.get("href", "")
                    break
            link = link or (entry.find("a:id", ns).text if entry.find("a:id", ns) is not None else "")
            authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
            year = ""
            published = entry.findtext("a:published", "", ns) or ""
            year = published[:4]
            out.append(
                f"Title: {title}\nAuthors: {', '.join(authors)}\nYear: {year}\n"
                f"URL: {link}\nAbstract: {summary[:800]}"
            )
        return "\n\n---\n\n".join(out) if out else "No papers found."
    except Exception as exc:  # noqa: BLE001
        return f"arXiv search error: {exc}"


# ---------------------------------------------------------------------------
# github — gh CLI or api.github.com
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="🐙")
async def github(query_type: str, target: str) -> str:
    """Query GitHub read-only data.

    :param query_type: "pr" | "issue" | "run" | "repo" | "api".
    :param target: owner/repo[#n] or URL for pr/issue; owner/repo for run/repo;
        raw api path (e.g. "users/octocat") for api.
    """
    q = (query_type or "").strip().lower()
    t = (target or "").strip()
    if not q or not t:
        return "query_type and target are required."

    def _gh(args: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["gh", *args], capture_output=True, text=True, timeout=30
            )
            return proc.returncode, (proc.stdout or proc.stderr)[:12000]
        except FileNotFoundError:
            return 127, "gh CLI not installed"

    if q == "pr":
        code, out = _gh(["pr", "view", t, "--json", "title,body,state,author,url,comments"])
    elif q == "issue":
        code, out = _gh(["issue", "view", t, "--json", "title,body,state,author,url,comments"])
    elif q == "run":
        code, out = _gh(["run", "list", "--repo", t, "--limit", "10", "--json", "databaseId,name,status,conclusion,url"])
    elif q == "repo":
        code, out = _gh(["repo", "view", t, "--json", "name,description,stargazerCount,forkCount,url,defaultBranchRef"])
    elif q == "api":
        code, out = _gh(["api", t])
    else:
        return f"Unknown query_type {q!r}; use pr|issue|run|repo|api."
    return out if code == 0 else f"gh exited {code}: {out}"


# ---------------------------------------------------------------------------
# exec — run code in the workspace (python or shell)
# ---------------------------------------------------------------------------


@tool(mutating=True, tool_icon="▶️")
async def exec(code: str, language: str = "python", timeout: int = 30) -> str:
    """Execute code and return stdout/stderr. Runs inside the workspace dir.

    :param code: Source code (python) or shell commands.
    :param language: "python" (default) or "bash".
    :param timeout: Seconds before the process is killed (max 120).
    """
    timeout = max(1, min(int(timeout), 120))
    lang = (language or "python").strip().lower()
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        if lang in ("python", "py"):
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", code,
                cwd=root, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        elif lang in ("bash", "sh", "shell"):
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", code,
                cwd=root, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            return f"Unsupported language {language!r}; use python or bash."
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"Error: timed out after {timeout}s"
        stdout = out.decode("utf-8", "replace")[:12000]
        stderr = err.decode("utf-8", "replace")[:4000]
        return (stdout + (("\n[stderr]\n" + stderr) if stderr else "")) or "(no output)"
    except Exception as exc:  # noqa: BLE001
        return f"exec error: {exc}"




# ---------------------------------------------------------------------------
# LLM-backed tools: reason / brainstorm — tool-less sub-agents
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="🧠")
async def reason(question: str, context: str = "") -> str:
    """Run a dedicated deep-reasoning pass on a question.

    :param question: The question or problem to reason about.
    :param context: Optional extra context to reason over.
    """
    prompt = question if not context else f"{question}\n\nContext:\n{context}"
    agent = _subagent(
        None,
        "You are a careful reasoning engine. Think step by step and give a "
        "precise, well-structured answer. No tools, no preamble.",
        "reasoner",
    )
    try:
        result = await Runner.run(agent, prompt, max_turns=5)
        return str(result.final_output or "(no output)")
    except Exception as exc:  # noqa: BLE001
        return f"reason error: {exc}"


@tool(mutating=False, tool_icon="💡")
async def brainstorm(topic: str, constraints: str = "") -> str:
    """Breadth-first idea exploration with rationale for each idea.

    :param topic: The subject to brainstorm about.
    :param constraints: Optional constraints (audience, budget, timeframe…).
    """
    prompt = f"Brainstorm ideas about: {topic}"
    if constraints:
        prompt += f"\nConstraints: {constraints}"
    agent = _subagent(
        None,
        "You are a creative brainstorming partner. Generate a broad set of "
        "distinct ideas; for each give a one-line rationale. Organize by theme.",
        "brainstormer",
    )
    try:
        result = await Runner.run(agent, prompt, max_turns=5)
        return str(result.final_output or "(no output)")
    except Exception as exc:  # noqa: BLE001
        return f"brainstorm error: {exc}"


# ---------------------------------------------------------------------------
# Notes — simple markdown/JSON store under data/notes (write_note, list_notebook,
# search_notes) and question_bank
# ---------------------------------------------------------------------------


def _notes_dir(settings: Settings) -> Path:
    path = settings.data_dir / "notes"
    path.mkdir(parents=True, exist_ok=True)
    return path




def _notes_dir_for_env() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve() / "notes"
    root.mkdir(parents=True, exist_ok=True)
    return root






@tool(mutating=True, tool_icon="📝")
def notes(action: str, title: str = "", content: str = "", tags: str = "",
          query: str = "") -> str:
    """The user's notebook — one tool for write, list, and search.

    Actions:
    - ``write``: save a markdown note (``title``, ``content``, optional
      comma-separated ``tags``).
    - ``list``: list all notes (id | title | tags).
    - ``search``: full-text search across notes (``query``).

    :param action: write|list|search.
    :param title: Note title (write).
    :param content: Markdown body (write).
    :param tags: Comma-separated tags (write).
    :param query: Search terms (search).
    """
    root = _notes_dir_for_env()
    action = (action or "list").lower()

    if action == "write":
        if not title:
            return "notes write needs a title."
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "note"
        note_id = f"{slug}-{uuid.uuid4().hex[:6]}"
        path = root / f"{note_id}.md"
        meta = (f"---\ntitle: {title}\ntags: {tags}\n"
                f"created: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n---\n\n")
        path.write_text(meta + content, encoding="utf-8")
        return f"Saved note '{title}' as {note_id}"

    if action == "search":
        q = query.strip().lower()
        if not q:
            return "notes search needs a query."
        hits = []
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if q in text.lower():
                idx = text.lower().find(q)
                snippet = text[max(0, idx - 80):idx + 200].replace("\n", " ")
                hits.append(f"[{path.stem}] …{snippet}…")
        return "\n\n".join(hits[:10]) if hits else "No matching notes."

    # list
    out = []
    for path in sorted(root.glob("*.md")):
        head = path.read_text(encoding="utf-8", errors="replace")[:600]
        title = path.stem
        m = re.search(r"^title:\s*(.+)$", head, re.M)
        if m:
            title = m.group(1).strip()
        tags = ""
        m = re.search(r"^tags:\s*(.+)$", head, re.M)
        if m:
            tags = m.group(1).strip()
        out.append(f"{path.stem} | {title} | tags: {tags}")
    return "\n".join(out) if out else "No notes saved yet."


@tool(mutating=True, tool_icon="❓")
def question_bank(action: str, question: str = "", answer: str = "", topic: str = "") -> str:
    """Manage a personal Q&A bank (add | list | search).

    :param action: "add", "list", or "search".
    :param question: The question text.
    :param answer: The answer (for add).
    :param topic: Optional topic tag.
    """
    root = _notes_dir_for_env().parent / "question_bank.json"
    root.parent.mkdir(parents=True, exist_ok=True)
    bank = json.loads(root.read_text()) if root.exists() else []
    action = (action or "").strip().lower()
    if action == "add":
        if not question.strip():
            return "question is required for add."
        bank.append({"q": question, "a": answer, "topic": topic,
                     "created": time.time()})
        root.write_text(json.dumps(bank, ensure_ascii=False, indent=1))
        return f"Saved question to bank ({len(bank)} total)."
    if action == "list":
        return "\n".join(f"- {e['q']}" + (f" [{e.get('topic','')}]" if e.get("topic") else "")
                         for e in bank[-50:]) or "Question bank is empty."
    if action == "search":
        q = question.strip().lower() or topic.strip().lower()
        hits = [e for e in bank if q in (e["q"] + " " + e.get("a", "")).lower()]
        return "\n\n".join(f"Q: {e['q']}\nA: {e.get('a','')}" for e in hits[:10]) or "No matches."
    return 'action must be "add", "list", or "search".'


# ---------------------------------------------------------------------------
# memory — tiny persistent memory (read_memory / write_memory)
# ---------------------------------------------------------------------------


def _memory_path() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "memory.json"




@tool(mutating=True, tool_icon="🧠")
def memory(action: str, query: str = "", fact: str = "",
           category: str = "general") -> str:
    """Long-term memory about the learner — one tool for read + write.

    Actions:
    - ``read``: return stored memories (``query`` filters by substring).
    - ``write``: store a durable fact (``fact`` one sentence, ``category``
      optional e.g. preference|goal|background).

    :param action: read|write.
    :param query: Substring filter for read.
    :param fact: Fact to store for write.
    :param category: Category for write.
    """
    path = _memory_path()
    action = (action or "read").lower()
    if action == "write":
        if not fact:
            return "memory write needs a fact."
        memories = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        memories.append({"fact": fact, "category": category, "created": time.time()})
        path.write_text(json.dumps(memories, ensure_ascii=False, indent=1))
        return f"Remembered: {fact}"
    # read
    if not path.exists():
        return "No memories stored yet."
    memories = json.loads(path.read_text(encoding="utf-8"))
    q = query.strip().lower()
    if q:
        memories = [m for m in memories if q in json.dumps(m, ensure_ascii=False).lower()]
    return json.dumps(memories[-50:], ensure_ascii=False, indent=1) or "[]"


# ---------------------------------------------------------------------------
# imagegen — pollinations.ai (no key required)
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="🎨")
async def imagegen(prompt: str, width: int = 1024, height: int = 1024) -> str:
    """Generate an image from a text prompt. Returns a URL to the image.

    :param prompt: Image description.
    :param width: Width in pixels.
    :param height: Height in pixels.
    """
    from urllib.parse import quote

    url = (
        f"https://image.pollinations.ai/prompt/{quote(prompt)}"
        f"?width={width}&height={height}&nologo=true"
    )
    try:
        async with httpx.AsyncClient(timeout=60.0, headers=_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return f"Generated image URL: {url}\n(Content type: {resp.headers.get('content-type')})"
    except Exception as exc:  # noqa: BLE001
        return f"imagegen error: {exc}"


# ---------------------------------------------------------------------------
# ask_user — pause the turn and ask the learner a question
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="❓")
def ask_user(question: str, options: str = "", context: str = "") -> str:
    """Ask the learner a question and wait for their reply.

    Use when you need clarification, a decision, or confirmation before
    continuing. The learner's next message is the answer.

    :param question: The question to ask.
    :param options: Optional comma-separated choices.
    :param context: Optional background explaining why you're asking.
    """
    parts = [f"QUESTION: {question}"]
    if options:
        parts.append(f"OPTIONS: {options}")
    if context:
        parts.append(f"CONTEXT: {context}")
    parts.append(
        "INSTRUCTION: Present this question to the learner clearly and stop. "
        "Their reply will arrive as the next user message — do not continue "
        "the task until then."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# learning — single tool covering the whole learning-state surface
# (upstream learning_status + learning_update + journal merged)
# ---------------------------------------------------------------------------


def _learning_path() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "learning.json"


@tool(mutating=True, tool_icon="📚")
def learning(action: str, topic: str = "", note: str = "", status: str = "",
             minutes: int = 0) -> str:
    """Track the learner's progress across topics — one tool for all learning state.

    Actions:
    - ``status``: show overall progress and per-topic state.
    - ``update``: record progress on ``topic`` (status: learned|learning|weak,
      optional ``note`` and ``minutes`` spent).
    - ``journal``: append a free-form ``note`` to the learning journal.
    - ``weak``: list topics marked weak for review.

    :param action: One of status|update|journal|weak.
    :param topic: Topic name (for update).
    :param note: Free-text note (for update/journal).
    :param status: learned|learning|weak (for update).
    :param minutes: Minutes spent (for update).
    """
    path = _learning_path()
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        data = {"topics": {}, "journal": []}
    topics: dict[str, Any] = data.setdefault("topics", {})
    journal: list[dict[str, Any]] = data.setdefault("journal", [])

    action = (action or "status").lower()
    if action == "update":
        if not topic:
            return "learning update needs a topic."
        t = topics.setdefault(topic, {"status": "learning", "notes": [], "minutes": 0})
        if status:
            t["status"] = status
        if note:
            t["notes"].append({"at": time.time(), "note": note})
        t["minutes"] = int(t.get("minutes", 0)) + max(0, minutes)
        t["updated_at"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return f"Recorded: {topic} → {t['status']}"
    if action == "journal":
        if not note:
            return "journal needs a note."
        journal.append({"at": time.time(), "note": note})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return "Journal entry saved."
    if action == "weak":
        weak = {k: v for k, v in topics.items() if v.get("status") == "weak"}
        if not weak:
            return "No weak topics recorded."
        return "Weak topics:\n" + "\n".join(
            f"- {k} ({v.get('minutes', 0)}min)" for k, v in weak.items())
    # status
    if not topics:
        return "No learning progress recorded yet."
    lines = ["Learning status:"]
    for k, v in sorted(topics.items()):
        lines.append(f"- {k}: {v.get('status','?')} ({v.get('minutes',0)}min)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# workspace — single tool that controls the whole workspace
# (upstream workspace_read/list/present/export merged)
# ---------------------------------------------------------------------------


@tool(mutating=True, tool_icon="🗂️")
def workspace(action: str, path: str = "", content: str = "",
              query: str = "", max_chars: int = 12000) -> str:
    """Full control over the workspace directory — one tool for all file ops.

    Actions:
    - ``list``: list files under ``path`` (default root, non-recursive unless
      path ends with '/').
    - ``read``: read a UTF-8 file at ``path``.
    - ``write``: write ``content`` to ``path`` (creates parents).
    - ``search``: substring-search file contents for ``query`` under ``path``.
    - ``delete``: delete file at ``path``.
    - ``mkdir``: create directory ``path``.
    - ``info``: file size/mtime for ``path``.

    Paths are workspace-relative; ``..`` escapes are rejected.

    :param action: list|read|write|search|delete|mkdir|info.
    :param path: Workspace-relative path.
    :param content: File content for write.
    :param query: Search substring for search.
    :param max_chars: Read truncation limit.
    """
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    root.mkdir(parents=True, exist_ok=True)

    def _resolve(p: str) -> Path:
        cand = (root / (p or ".").lstrip("/")).resolve()
        if cand != root and root not in cand.parents:
            raise ValueError(f"path escapes workspace: {p}")
        return cand

    action = (action or "list").lower()
    try:
        if action == "list":
            base = _resolve(path or ".")
            if not base.is_dir():
                return f"Not a directory: {path}"
            entries = sorted(base.iterdir(), key=lambda e: (e.is_file(), e.name))
            lines = []
            for e in entries[:200]:
                rel = e.relative_to(root)
                lines.append(f"{'📁' if e.is_dir() else '📄'} {rel}")
            return "\n".join(lines) or "(empty)"
        if action == "read":
            f = _resolve(path)
            if not f.is_file():
                return f"Not a file: {path}"
            return f.read_text(encoding="utf-8", errors="replace")[:max_chars]
        if action == "write":
            f = _resolve(path)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} chars to {path}"
        if action == "search":
            base = _resolve(path or ".")
            hits = []
            for f in base.rglob("*") if base.is_dir() else [base]:
                if not f.is_file() or f.stat().st_size > 2_000_000:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                if query.lower() in text.lower():
                    hits.append(str(f.relative_to(root)))
                    if len(hits) >= 20:
                        break
            return "\n".join(hits) or "No matches."
        if action == "delete":
            f = _resolve(path)
            if f.is_file():
                f.unlink()
                return f"Deleted {path}"
            if f.is_dir():
                import shutil
                shutil.rmtree(f)
                return f"Deleted dir {path}"
            return f"Not found: {path}"
        if action == "mkdir":
            _resolve(path).mkdir(parents=True, exist_ok=True)
            return f"Created {path}"
        if action == "info":
            f = _resolve(path)
            if not f.exists():
                return f"Not found: {path}"
            st = f.stat()
            return f"{path}: {'dir' if f.is_dir() else 'file'}, {st.st_size} bytes, mtime {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}"
        return f"Unknown workspace action: {action}"
    except Exception as exc:  # noqa: BLE001
        return f"workspace {action} error: {exc}"


# ---------------------------------------------------------------------------
# quiz — generate and grade quizzes in one tool
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="📝")
async def quiz(action: str, topic: str = "", question: str = "",
               answer: str = "", num_questions: int = 5) -> str:
    """Generate or grade quiz questions — one tool for the whole quiz flow.

    Actions:
    - ``generate``: create ``num_questions`` questions on ``topic``.
      Returns questions with answers for the tutor to ask the learner.
    - ``grade``: grade the learner's ``answer`` to ``question``; returns
      correct/incorrect + feedback.

    :param action: generate|grade.
    :param topic: Subject for generate.
    :param question: Question text for grade.
    :param answer: Learner's answer for grade.
    :param num_questions: How many questions to generate.
    """
    action = (action or "generate").lower()
    if action == "grade":
        if not question or not answer:
            return "quiz grade needs question and answer."
        prompt = (
            "Grade this quiz answer. Reply JSON: "
            '{"correct":bool,"score":0-100,"feedback":"one sentence"}\n\n'
            f"Question: {question}\nAnswer: {answer}"
        )
        try:
            return _heuristic_grade(prompt)
        except Exception as exc:  # noqa: BLE001
            return f"quiz grade error: {exc}"
    # generate — deterministic template quiz (no LLM dependency)
    if not topic:
        return "quiz generate needs a topic."
    qs = [
        f"1. Define {topic} in one sentence.",
        f"2. What is the most important property of {topic}?",
        f"3. Give a real-world example of {topic}.",
        f"4. How does {topic} relate to a closely related concept?",
        f"5. What would happen if {topic} didn't exist?",
    ][: max(1, min(num_questions, 10))]
    return "Quiz on " + topic + ":\n" + "\n".join(qs) + \
        "\n\nINSTRUCTION: Ask these one at a time. After each answer, call quiz(action='grade')."


# ---------------------------------------------------------------------------
# mastery — single tool covering the whole Guided Learning surface
# (upstream mastery_* family merged)
# ---------------------------------------------------------------------------


def _mastery_path() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "mastery.json"


@tool(mutating=True, tool_icon="🎯")
def mastery(action: str, topic: str = "", question: str = "",
            answer: str = "", score: int = -1, note: str = "") -> str:
    """Guided Learning mastery — one tool for the whole mastery loop.

    Actions:
    - ``topics``: list topics and their mastery state.
    - ``start``: begin a mastery session on ``topic``.
    - ``status``: current session state.
    - ``quiz``: get the next review question for ``topic``.
    - ``grade``: record learner's ``answer`` to ``question`` with ``score``
      (0-100, -1 = ungraded); updates mastery level.
    - ``note``: attach a free-form ``note`` to ``topic``.
    - ``leave``: end the current session.

    :param action: topics|start|status|quiz|grade|note|leave.
    :param topic: Topic name.
    :param question: Question being answered (grade).
    :param answer: Learner's answer (grade).
    :param score: 0-100 correctness (grade).
    :param note: Free-text note.
    """
    path = _mastery_path()
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        data = {"topics": {}, "session": None}
    topics: dict[str, Any] = data.setdefault("topics", {})
    session = data.get("session")

    action = (action or "status").lower()
    if action == "topics":
        if not topics:
            return "No mastery topics yet. Use mastery(action='start', topic='...')."
        return "Mastery topics:\n" + "\n".join(
            f"- {k}: level {v.get('level', 0)}/5, {v.get('reviews', 0)} reviews"
            for k, v in topics.items())
    if action == "start":
        if not topic:
            return "mastery start needs a topic."
        topics.setdefault(topic, {"level": 0, "reviews": 0, "notes": []})
        data["session"] = {"topic": topic, "started_at": time.time(), "turn": 0}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return f"Started mastery session on {topic}. Ask a diagnostic question."
    if action == "status":
        return f"Session: {session} " if session else "No active session. " + \
            f"Topics: {len(topics)}"
    if action == "quiz":
        t = session and topics.get(session.get("topic"))
        if not t:
            return "No active session — mastery(action='start', topic='...') first."
        lvl = t.get("level", 0)
        prompts = [
            f"Explain {session['topic']} from scratch.",
            f"Give a non-trivial example of {session['topic']}.",
            f"What are the edge cases of {session['topic']}?",
            f"Compare {session['topic']} with a related concept.",
            f"Teach {session['topic']} to a beginner in 3 steps.",
        ]
        q = prompts[min(lvl, len(prompts) - 1)]
        return f"QUIZ: {q}\n(INSTRUCTION: ask the learner, then call mastery(action='grade').)"
    if action == "grade":
        if not session or not question:
            return "mastery grade needs an active session and question."
        t = topics.setdefault(session["topic"], {"level": 0, "reviews": 0, "notes": []})
        t["reviews"] = int(t.get("reviews", 0)) + 1
        if score >= 0:
            # level 0-5: correct answers raise, wrong lower (min 0)
            delta = 1 if score >= 70 else (-1 if score < 40 else 0)
            t["level"] = max(0, min(5, int(t.get("level", 0)) + delta))
        if note:
            t.setdefault("notes", []).append({"at": time.time(), "note": note})
        t["updated_at"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return (f"Recorded: level {t['level']}/5 after {t['reviews']} reviews "
                f"on {session['topic']}.")
    if action == "note":
        tname = topic or (session or {}).get("topic")
        t = topics.setdefault(tname or "general", {"level": 0, "reviews": 0, "notes": []})
        t.setdefault("notes", []).append({"at": time.time(), "note": note})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return "Note saved."
    if action == "leave":
        data["session"] = None
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return "Mastery session ended."
    return f"Unknown mastery action: {action}"


# ---------------------------------------------------------------------------
# video — render an HTML/Remotion composition into a real video file
# ---------------------------------------------------------------------------


@tool(mutating=True, tool_icon="🎬")
def video(action: str, title: str = "", html: str = "",
          script: str = "", composition: str = "") -> str:
    """Create a video from an HTML/Remotion composition — one tool for the flow.

    Actions:
    - ``scaffold``: write a Remotion project skeleton into the workspace
      ``videos/<slug>/`` and return its path.
    - ``render``: render composition ``composition`` (default ``Main``) to
      ``videos/<slug>/out.mp4`` using ``npx remotion render``. Requires the
      scaffold to exist. Returns the output path or error.
    - ``list``: list rendered videos in the workspace.

    :param action: scaffold|render|list.
    :param title: Video title (used for the slug).
    :param html: Optional HTML/TSX body for the composition.
    :param script: Optional narration script text.
    :param composition: Remotion composition id for render.
    """
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    vdir = root / "videos"
    vdir.mkdir(parents=True, exist_ok=True)

    def _slug() -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (title or "video").lower()).strip("-") or "video"
        return f"{s}-{uuid.uuid4().hex[:6]}"

    action = (action or "scaffold").lower()
    if action == "list":
        outs = sorted(vdir.glob("*/out.mp4")) + sorted(vdir.glob("*/*.mp4"))
        return "\n".join(str(p.relative_to(root)) for p in outs) or "No videos yet."
    if action == "scaffold":
        slug = _slug()
        proj = vdir / slug
        (proj / "src").mkdir(parents=True, exist_ok=True)
        body = html or (
            "import {AbsoluteFill} from 'remotion';\n"
            "export const Main = () => (\n"
            f"  <AbsoluteFill style={{{{background:'#0b1020',color:'#fff',"
            "display:'flex',alignItems:'center',justifyContent:'center',"
            "fontSize:64}}}>\n"
            f"    <div>{(title or 'Video').replace('<','&lt;')}</div>\n"
            "  </AbsoluteFill>\n);\n"
        )
        (proj / "src" / "Composition.tsx").write_text(body)
        (proj / "src" / "index.ts").write_text(
            "import {registerRoot} from 'remotion';\n"
            "import {Composition} from 'remotion';\n"
            "import {Main} from './Composition';\n"
            "registerRoot(() => <Composition id=\"Main\" component={Main} "
            "durationInFrames={150} fps={30} width={1920} height={1080} />);\n"
        )
        (proj / "package.json").write_text(json.dumps({
            "name": slug, "private": True,
            "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0",
                             "remotion": "^4.0.0"},
            "scripts": {"render": "remotion render src/index.ts Main out.mp4"},
        }, indent=2))
        return f"Scaffolded Remotion project at videos/{slug}/ — run video(action='render', title='{slug}') to produce out.mp4"
    if action == "render":
        slug = title or ""
        proj = vdir / slug
        if not (proj / "package.json").exists():
            # find most recent scaffold if title omitted
            cands = sorted(vdir.glob("*/package.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not cands:
                return "No video project — run video(action='scaffold') first."
            proj = cands[0].parent
            slug = proj.name
        comp = composition or "Main"
        out = proj / "out.mp4"
        cmd = ["npx", "--yes", "remotion", "render", "src/index.ts", comp, "out.mp4"]
        try:
            proc = subprocess.run(cmd, cwd=proj, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            return "npx/remotion not available — scaffold exists at videos/%s but render needs Node toolchain." % slug
        except subprocess.TimeoutExpired:
            return "render timed out after 600s."
        if proc.returncode != 0 or not out.exists():
            return f"render failed: {proc.stderr[-500:] or proc.stdout[-500:]}"
        return f"Video rendered: videos/{slug}/out.mp4 ({out.stat().st_size} bytes)"
    return f"Unknown video action: {action}"


# ---------------------------------------------------------------------------
# apply_patch — ported from vtx coding_agent/tools/apply_patch.py
# Atomic file edits: ops update/create/delete/move, validated fully before
# any byte is written. Replaces the simple write_file tool.
# ---------------------------------------------------------------------------

_APPLY_PATCH_OPS = (
    '{"op":"update","path":"a.py","old":"exact text","new":"replacement"} | '
    '{"op":"update","path":"a.py","edits":[{"old":"..","new":"..","replace_all":false}]} | '
    '{"op":"create","path":"b.txt","content":"..."} | '
    '{"op":"delete","path":"c.txt"} | '
    '{"op":"move","path":"a","new_path":"b"}'
)


def _patch_root() -> Path:
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _patch_resolve(root: Path, p: str) -> Path:
    cand = (root / (p or "").lstrip("/")).resolve()
    if cand != root and root not in cand.parents:
        raise ValueError(f"path escapes workspace: {p}")
    return cand


@tool(mutating=True, tool_icon="🩹")
def apply_patch(ops_json: str) -> str:
    """Apply file edits atomically — the ONLY tool for file modifications.

    Pass a JSON array of ops (see format below). Every op is validated
    against an in-memory view first; if ANY op fails, NOTHING is written.

    Ops:
    - ``{"op":"update","path":P,"old":O,"new":N}`` — replace first exact
      occurrence of O (must match whitespace) with N; add
      ``"replace_all":true`` for all occurrences.
    - ``{"op":"update","path":P,"edits":[{"old":O,"new":N,"replace_all":bool}]}``
      — several edits to one file.
    - ``{"op":"create","path":P,"content":C}`` — new file (fails if exists).
    - ``{"op":"delete","path":P}`` — remove file.
    - ``{"op":"move","path":P,"new_path":Q}`` — rename.

    :param ops_json: JSON array of ops, e.g. [{"op":"update","path":"a.py",
        "old":"x = 1","new":"x = 2"}]
    """
    try:
        ops = json.loads(ops_json)
    except Exception as exc:  # noqa: BLE001
        return f"apply_patch: invalid JSON ops: {exc}"
    if not isinstance(ops, list) or not ops:
        return "apply_patch: ops must be a non-empty JSON array."

    root = _patch_root()
    _DELETED = object()
    state: dict[Path, Any] = {}

    def current(path: Path) -> str | None:
        if path in state:
            v = state[path]
            return None if v is _DELETED else v
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc

    def exists(path: Path) -> bool:
        if path in state:
            return state[path] is not _DELETED
        return path.exists()

    def mark(path: Path, content: str | None) -> None:
        state[path] = _DELETED if content is None else content

    try:
        for idx, raw in enumerate(ops):
            if not isinstance(raw, dict):
                raise ValueError(f"op {idx + 1}: not an object")
            op = raw.get("op")
            path = _patch_resolve(root, str(raw.get("path") or ""))
            label = f"op {idx + 1} ({op} {raw.get('path')})"

            if op == "update":
                edits = raw.get("edits")
                if edits is None:
                    edits = [{"old": raw.get("old"), "new": raw.get("new"),
                              "replace_all": raw.get("replace_all", False)}]
                content = current(path)
                if content is None:
                    raise ValueError(f"{label}: file does not exist")
                new_content = content
                for j, e in enumerate(edits):
                    old, new = e.get("old"), e.get("new")
                    if old is None or new is None:
                        raise ValueError(f"{label} edit {j + 1}: old/new required")
                    if old == new:
                        raise ValueError(f"{label} edit {j + 1}: old == new")
                    count = new_content.count(old)
                    if count == 0:
                        raise ValueError(f"{label} edit {j + 1}: 'old' not found")
                    if count > 1 and not e.get("replace_all"):
                        raise ValueError(
                            f"{label} edit {j + 1}: 'old' matches {count} times "
                            "— set replace_all or make it unique")
                    new_content = (new_content.replace(old, new)
                                   if e.get("replace_all")
                                   else new_content.replace(old, new, 1))
                mark(path, new_content)
            elif op == "create":
                if exists(path):
                    raise ValueError(f"{label}: file already exists")
                content = raw.get("content")
                if content is None:
                    raise ValueError(f"{label}: 'content' required")
                mark(path, str(content))
            elif op == "delete":
                if not exists(path):
                    raise ValueError(f"{label}: file does not exist")
                mark(path, None)
            elif op == "move":
                new_path = raw.get("new_path")
                if not new_path:
                    raise ValueError(f"{label}: 'new_path' required")
                content = current(path)
                if content is None:
                    raise ValueError(f"{label}: file does not exist")
                dest = _patch_resolve(root, str(new_path))
                if dest.exists() and dest not in state:
                    raise ValueError(f"{label}: destination exists")
                state[dest] = state.get(path, content)
                mark(path, None)
            else:
                raise ValueError(f"{label}: unknown op {op!r}")
    except Exception as exc:  # noqa: BLE001
        return f"apply_patch failed (no changes written): {exc}"

    # All ops validated — apply atomically.
    applied: list[str] = []
    try:
        for path, value in state.items():
            if value is _DELETED:
                if path.exists():
                    if path.is_dir():
                        import shutil
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                applied.append(f"deleted {path.relative_to(root)}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
                applied.append(f"wrote {path.relative_to(root)} ({len(value)} chars)")
    except Exception as exc:  # noqa: BLE001
        return f"apply_patch write failed mid-apply: {exc}"
    return "apply_patch applied:\n" + "\n".join(applied)


# ---------------------------------------------------------------------------
# web — single tool for search + fetch (vtx WebTool semantics: one tool,
# mode='search' queries, mode='fetch' downloads a URL's text)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# web — vtx WebTool port: Exa MCP search + Jina Reader fetch, single tool
# ---------------------------------------------------------------------------

_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
_JINA_PREFIX = "https://r.jina.ai/"
_WEB_TIMEOUT = 25.0
_MAX_FETCH_CHARS = 30000

_JINA_HEADER_RE = re.compile(
    r"^(Title|URL Source|Published Time|Warning|Markdown Content):.*\n?",
    re.MULTILINE,
)


def _strip_jina_header(text: str) -> str:
    marker = text.find("Markdown Content:")
    if marker != -1:
        return text[marker + len("Markdown Content:"):].strip()
    return _JINA_HEADER_RE.sub("", text).strip()


async def _exa_search(query: str, num_results: int = 8) -> tuple[str | None, str | None]:
    """POST a JSON-RPC tools/call to the Exa MCP endpoint (no API key)."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {
                "query": query,
                "type": "auto",
                "numResults": num_results,
                "livecrawl": "fallback",
            },
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_WEB_TIMEOUT) as client:
            resp = await client.post(_EXA_MCP_URL, headers=headers, json=payload)
            resp.raise_for_status()
    except httpx.TimeoutException:
        return None, f"Exa timed out after {_WEB_TIMEOUT}s"
    except httpx.HTTPStatusError as exc:
        return None, f"Exa API error {exc.response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"Exa request failed: {exc}"

    def _extract(data: dict) -> str | None:
        content = data.get("result", {}).get("content", [])
        if content and isinstance(content, list):
            return content[0].get("text")
        return None

    # SSE-streamed response: first data line with content wins.
    for line in resp.text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        text = _extract(data)
        if text:
            return text, None
    try:
        text = _extract(resp.json())
        if text:
            return text, None
    except Exception:  # noqa: BLE001
        pass
    return None, "No results returned from Exa API"


async def _jina_fetch(url: str) -> tuple[str | None, str | None]:
    """Fetch a URL as markdown via Jina Reader (r.jina.ai)."""
    try:
        async with httpx.AsyncClient(timeout=_WEB_TIMEOUT) as client:
            resp = await client.get(_JINA_PREFIX + url)
    except httpx.TimeoutException:
        return None, f"Fetch timed out after {_WEB_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"Fetch failed: {exc}"
    if resp.status_code != 200:
        return None, f"Fetch failed: Jina Reader HTTP {resp.status_code}"
    text = _strip_jina_header(resp.text)
    if not text or "<html" in text[:200].lower():
        return None, "Fetch returned no readable content"
    if len(text) > _MAX_FETCH_CHARS:
        text = text[:_MAX_FETCH_CHARS] + f"\n[truncated at {_MAX_FETCH_CHARS} chars]"
    return text, None


@tool(mutating=False, tool_icon="🌐")
async def web(query: str, mode: str = "search", max_results: int = 8,
              max_chars: int = _MAX_FETCH_CHARS) -> str:
    """Web search (Exa) or URL fetch (Jina Reader) — one tool for both.

    ``mode='search'`` (default): query the web, returns titles/URLs/snippets.
    ``mode='fetch'``: download a URL's readable markdown text.
    Needs internet.

    :param query: Search query, or a URL when mode='fetch'.
    :param mode: 'search' (default) or 'fetch'.
    :param max_results: Max search results (Exa returns up to ~8-10).
    :param max_chars: Fetch truncation limit.
    """
    mode = (mode or "search").lower()
    if mode == "fetch":
        url = query.strip()
        if not url.startswith(("http://", "https://")):
            return "mode='fetch' requires an http(s) URL"
        text, err = await _jina_fetch(url)
        return err or (text or "")[:max_chars] if text else (err or "empty")
    text, err = await _exa_search(query, max_results)
    return err or text or "No results."


# ---------------------------------------------------------------------------
# paper_search — arXiv
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="📄")
async def paper_search(
    query: str, max_results: int = 5, sort_by: str = "relevance"
) -> str:
    """Search arXiv for research papers.

    :param query: Search keywords (arXiv query syntax supported).
    :param max_results: Number of papers to return (max 20).
    :param sort_by: "relevance" or "date".
    """
    import xml.etree.ElementTree as ET

    query = (query or "").strip()
    if not query:
        return "Empty query."
    max_results = max(1, min(int(max_results), 20))
    sort = "submittedDate" if sort_by == "date" else "relevance"
    api = (
        "https://export.arxiv.org/api/query?search_query="
        + httpx.QueryParams({"search_query": f"all:{query}", "start": 0,
                             "max_results": max_results, "sortBy": sort,
                             "sortOrder": "descending"})
    )
    # httpx.QueryParams returns str-like; build manually to keep it simple
    from urllib.parse import urlencode

    api = "https://export.arxiv.org/api/query?" + urlencode(
        {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": sort,
            "sortOrder": "descending",
        }
    )
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            resp = await client.get(api)
            resp.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.text)
        out = []
        for entry in root.findall("a:entry", ns):
            title = " ".join((entry.findtext("a:title", "", ns) or "").split())
            summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())
            link = ""
            for ln in entry.findall("a:link", ns):
                if ln.get("title") == "pdf" or ln.get("rel") == "related":
                    link = ln.get("href", "")
                    break
            link = link or (entry.find("a:id", ns).text if entry.find("a:id", ns) is not None else "")
            authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
            year = ""
            published = entry.findtext("a:published", "", ns) or ""
            year = published[:4]
            out.append(
                f"Title: {title}\nAuthors: {', '.join(authors)}\nYear: {year}\n"
                f"URL: {link}\nAbstract: {summary[:800]}"
            )
        return "\n\n---\n\n".join(out) if out else "No papers found."
    except Exception as exc:  # noqa: BLE001
        return f"arXiv search error: {exc}"


# ---------------------------------------------------------------------------
# github — gh CLI or api.github.com
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="🐙")
async def github(query_type: str, target: str) -> str:
    """Query GitHub read-only data.

    :param query_type: "pr" | "issue" | "run" | "repo" | "api".
    :param target: owner/repo[#n] or URL for pr/issue; owner/repo for run/repo;
        raw api path (e.g. "users/octocat") for api.
    """
    q = (query_type or "").strip().lower()
    t = (target or "").strip()
    if not q or not t:
        return "query_type and target are required."

    def _gh(args: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["gh", *args], capture_output=True, text=True, timeout=30
            )
            return proc.returncode, (proc.stdout or proc.stderr)[:12000]
        except FileNotFoundError:
            return 127, "gh CLI not installed"

    if q == "pr":
        code, out = _gh(["pr", "view", t, "--json", "title,body,state,author,url,comments"])
    elif q == "issue":
        code, out = _gh(["issue", "view", t, "--json", "title,body,state,author,url,comments"])
    elif q == "run":
        code, out = _gh(["run", "list", "--repo", t, "--limit", "10", "--json", "databaseId,name,status,conclusion,url"])
    elif q == "repo":
        code, out = _gh(["repo", "view", t, "--json", "name,description,stargazerCount,forkCount,url,defaultBranchRef"])
    elif q == "api":
        code, out = _gh(["api", t])
    else:
        return f"Unknown query_type {q!r}; use pr|issue|run|repo|api."
    return out if code == 0 else f"gh exited {code}: {out}"


# ---------------------------------------------------------------------------
# exec — run code in the workspace (python or shell)
# ---------------------------------------------------------------------------


@tool(mutating=True, tool_icon="▶️")
async def exec(code: str, language: str = "python", timeout: int = 30) -> str:
    """Execute code and return stdout/stderr. Runs inside the workspace dir.

    :param code: Source code (python) or shell commands.
    :param language: "python" (default) or "bash".
    :param timeout: Seconds before the process is killed (max 120).
    """
    timeout = max(1, min(int(timeout), 120))
    lang = (language or "python").strip().lower()
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        if lang in ("python", "py"):
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", code,
                cwd=root, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        elif lang in ("bash", "sh", "shell"):
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", code,
                cwd=root, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            return f"Unsupported language {language!r}; use python or bash."
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"Error: timed out after {timeout}s"
        stdout = out.decode("utf-8", "replace")[:12000]
        stderr = err.decode("utf-8", "replace")[:4000]
        return (stdout + (("\n[stderr]\n" + stderr) if stderr else "")) or "(no output)"
    except Exception as exc:  # noqa: BLE001
        return f"exec error: {exc}"




# ---------------------------------------------------------------------------
# LLM-backed tools: reason / brainstorm — tool-less sub-agents
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="🧠")
async def reason(question: str, context: str = "") -> str:
    """Run a dedicated deep-reasoning pass on a question.

    :param question: The question or problem to reason about.
    :param context: Optional extra context to reason over.
    """
    prompt = question if not context else f"{question}\n\nContext:\n{context}"
    agent = _subagent(
        None,
        "You are a careful reasoning engine. Think step by step and give a "
        "precise, well-structured answer. No tools, no preamble.",
        "reasoner",
    )
    try:
        result = await Runner.run(agent, prompt, max_turns=5)
        return str(result.final_output or "(no output)")
    except Exception as exc:  # noqa: BLE001
        return f"reason error: {exc}"


@tool(mutating=False, tool_icon="💡")
async def brainstorm(topic: str, constraints: str = "") -> str:
    """Breadth-first idea exploration with rationale for each idea.

    :param topic: The subject to brainstorm about.
    :param constraints: Optional constraints (audience, budget, timeframe…).
    """
    prompt = f"Brainstorm ideas about: {topic}"
    if constraints:
        prompt += f"\nConstraints: {constraints}"
    agent = _subagent(
        None,
        "You are a creative brainstorming partner. Generate a broad set of "
        "distinct ideas; for each give a one-line rationale. Organize by theme.",
        "brainstormer",
    )
    try:
        result = await Runner.run(agent, prompt, max_turns=5)
        return str(result.final_output or "(no output)")
    except Exception as exc:  # noqa: BLE001
        return f"brainstorm error: {exc}"


# ---------------------------------------------------------------------------
# Notes — simple markdown/JSON store under data/notes (write_note, list_notebook,
# search_notes) and question_bank
# ---------------------------------------------------------------------------


def _notes_dir(settings: Settings) -> Path:
    path = settings.data_dir / "notes"
    path.mkdir(parents=True, exist_ok=True)
    return path




def _notes_dir_for_env() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve() / "notes"
    root.mkdir(parents=True, exist_ok=True)
    return root






@tool(mutating=True, tool_icon="❓")
def question_bank(action: str, question: str = "", answer: str = "", topic: str = "") -> str:
    """Manage a personal Q&A bank (add | list | search).

    :param action: "add", "list", or "search".
    :param question: The question text.
    :param answer: The answer (for add).
    :param topic: Optional topic tag.
    """
    root = _notes_dir_for_env().parent / "question_bank.json"
    root.parent.mkdir(parents=True, exist_ok=True)
    bank = json.loads(root.read_text()) if root.exists() else []
    action = (action or "").strip().lower()
    if action == "add":
        if not question.strip():
            return "question is required for add."
        bank.append({"q": question, "a": answer, "topic": topic,
                     "created": time.time()})
        root.write_text(json.dumps(bank, ensure_ascii=False, indent=1))
        return f"Saved question to bank ({len(bank)} total)."
    if action == "list":
        return "\n".join(f"- {e['q']}" + (f" [{e.get('topic','')}]" if e.get("topic") else "")
                         for e in bank[-50:]) or "Question bank is empty."
    if action == "search":
        q = question.strip().lower() or topic.strip().lower()
        hits = [e for e in bank if q in (e["q"] + " " + e.get("a", "")).lower()]
        return "\n\n".join(f"Q: {e['q']}\nA: {e.get('a','')}" for e in hits[:10]) or "No matches."
    return 'action must be "add", "list", or "search".'


# ---------------------------------------------------------------------------
# memory — tiny persistent memory (read_memory / write_memory)
# ---------------------------------------------------------------------------


def _memory_path() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "memory.json"




# ---------------------------------------------------------------------------
# imagegen — pollinations.ai (no key required)
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="🎨")
async def imagegen(prompt: str, width: int = 1024, height: int = 1024) -> str:
    """Generate an image from a text prompt. Returns a URL to the image.

    :param prompt: Image description.
    :param width: Width in pixels.
    :param height: Height in pixels.
    """
    from urllib.parse import quote

    url = (
        f"https://image.pollinations.ai/prompt/{quote(prompt)}"
        f"?width={width}&height={height}&nologo=true"
    )
    try:
        async with httpx.AsyncClient(timeout=60.0, headers=_HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return f"Generated image URL: {url}\n(Content type: {resp.headers.get('content-type')})"
    except Exception as exc:  # noqa: BLE001
        return f"imagegen error: {exc}"


# ---------------------------------------------------------------------------
# ask_user — pause the turn and ask the learner a question
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="❓")
def ask_user(question: str, options: str = "", context: str = "") -> str:
    """Ask the learner a question and wait for their reply.

    Use when you need clarification, a decision, or confirmation before
    continuing. The learner's next message is the answer.

    :param question: The question to ask.
    :param options: Optional comma-separated choices.
    :param context: Optional background explaining why you're asking.
    """
    parts = [f"QUESTION: {question}"]
    if options:
        parts.append(f"OPTIONS: {options}")
    if context:
        parts.append(f"CONTEXT: {context}")
    parts.append(
        "INSTRUCTION: Present this question to the learner clearly and stop. "
        "Their reply will arrive as the next user message — do not continue "
        "the task until then."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# learning — single tool covering the whole learning-state surface
# (upstream learning_status + learning_update + journal merged)
# ---------------------------------------------------------------------------


def _learning_path() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "learning.json"


@tool(mutating=True, tool_icon="📚")
def learning(action: str, topic: str = "", note: str = "", status: str = "",
             minutes: int = 0) -> str:
    """Track the learner's progress across topics — one tool for all learning state.

    Actions:
    - ``status``: show overall progress and per-topic state.
    - ``update``: record progress on ``topic`` (status: learned|learning|weak,
      optional ``note`` and ``minutes`` spent).
    - ``journal``: append a free-form ``note`` to the learning journal.
    - ``weak``: list topics marked weak for review.

    :param action: One of status|update|journal|weak.
    :param topic: Topic name (for update).
    :param note: Free-text note (for update/journal).
    :param status: learned|learning|weak (for update).
    :param minutes: Minutes spent (for update).
    """
    path = _learning_path()
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        data = {"topics": {}, "journal": []}
    topics: dict[str, Any] = data.setdefault("topics", {})
    journal: list[dict[str, Any]] = data.setdefault("journal", [])

    action = (action or "status").lower()
    if action == "update":
        if not topic:
            return "learning update needs a topic."
        t = topics.setdefault(topic, {"status": "learning", "notes": [], "minutes": 0})
        if status:
            t["status"] = status
        if note:
            t["notes"].append({"at": time.time(), "note": note})
        t["minutes"] = int(t.get("minutes", 0)) + max(0, minutes)
        t["updated_at"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return f"Recorded: {topic} → {t['status']}"
    if action == "journal":
        if not note:
            return "journal needs a note."
        journal.append({"at": time.time(), "note": note})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return "Journal entry saved."
    if action == "weak":
        weak = {k: v for k, v in topics.items() if v.get("status") == "weak"}
        if not weak:
            return "No weak topics recorded."
        return "Weak topics:\n" + "\n".join(
            f"- {k} ({v.get('minutes', 0)}min)" for k, v in weak.items())
    # status
    if not topics:
        return "No learning progress recorded yet."
    lines = ["Learning status:"]
    for k, v in sorted(topics.items()):
        lines.append(f"- {k}: {v.get('status','?')} ({v.get('minutes',0)}min)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# workspace — single tool that controls the whole workspace
# (upstream workspace_read/list/present/export merged)
# ---------------------------------------------------------------------------


@tool(mutating=True, tool_icon="🗂️")
def workspace(action: str, path: str = "", content: str = "",
              query: str = "", max_chars: int = 12000) -> str:
    """Full control over the workspace directory — one tool for all file ops.

    Actions:
    - ``list``: list files under ``path`` (default root, non-recursive unless
      path ends with '/').
    - ``read``: read a UTF-8 file at ``path``.
    - ``write``: write ``content`` to ``path`` (creates parents).
    - ``search``: substring-search file contents for ``query`` under ``path``.
    - ``delete``: delete file at ``path``.
    - ``mkdir``: create directory ``path``.
    - ``info``: file size/mtime for ``path``.

    Paths are workspace-relative; ``..`` escapes are rejected.

    :param action: list|read|write|search|delete|mkdir|info.
    :param path: Workspace-relative path.
    :param content: File content for write.
    :param query: Search substring for search.
    :param max_chars: Read truncation limit.
    """
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    root.mkdir(parents=True, exist_ok=True)

    def _resolve(p: str) -> Path:
        cand = (root / (p or ".").lstrip("/")).resolve()
        if cand != root and root not in cand.parents:
            raise ValueError(f"path escapes workspace: {p}")
        return cand

    action = (action or "list").lower()
    try:
        if action == "list":
            base = _resolve(path or ".")
            if not base.is_dir():
                return f"Not a directory: {path}"
            entries = sorted(base.iterdir(), key=lambda e: (e.is_file(), e.name))
            lines = []
            for e in entries[:200]:
                rel = e.relative_to(root)
                lines.append(f"{'📁' if e.is_dir() else '📄'} {rel}")
            return "\n".join(lines) or "(empty)"
        if action == "read":
            f = _resolve(path)
            if not f.is_file():
                return f"Not a file: {path}"
            return f.read_text(encoding="utf-8", errors="replace")[:max_chars]
        if action == "write":
            f = _resolve(path)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} chars to {path}"
        if action == "search":
            base = _resolve(path or ".")
            hits = []
            for f in base.rglob("*") if base.is_dir() else [base]:
                if not f.is_file() or f.stat().st_size > 2_000_000:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                if query.lower() in text.lower():
                    hits.append(str(f.relative_to(root)))
                    if len(hits) >= 20:
                        break
            return "\n".join(hits) or "No matches."
        if action == "delete":
            f = _resolve(path)
            if f.is_file():
                f.unlink()
                return f"Deleted {path}"
            if f.is_dir():
                import shutil
                shutil.rmtree(f)
                return f"Deleted dir {path}"
            return f"Not found: {path}"
        if action == "mkdir":
            _resolve(path).mkdir(parents=True, exist_ok=True)
            return f"Created {path}"
        if action == "info":
            f = _resolve(path)
            if not f.exists():
                return f"Not found: {path}"
            st = f.stat()
            return f"{path}: {'dir' if f.is_dir() else 'file'}, {st.st_size} bytes, mtime {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}"
        return f"Unknown workspace action: {action}"
    except Exception as exc:  # noqa: BLE001
        return f"workspace {action} error: {exc}"


# ---------------------------------------------------------------------------
# quiz — generate and grade quizzes in one tool
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="📝")
async def quiz(action: str, topic: str = "", question: str = "",
               answer: str = "", num_questions: int = 5) -> str:
    """Generate or grade quiz questions — one tool for the whole quiz flow.

    Actions:
    - ``generate``: create ``num_questions`` questions on ``topic``.
      Returns questions with answers for the tutor to ask the learner.
    - ``grade``: grade the learner's ``answer`` to ``question``; returns
      correct/incorrect + feedback.

    :param action: generate|grade.
    :param topic: Subject for generate.
    :param question: Question text for grade.
    :param answer: Learner's answer for grade.
    :param num_questions: How many questions to generate.
    """
    action = (action or "generate").lower()
    if action == "grade":
        if not question or not answer:
            return "quiz grade needs question and answer."
        prompt = (
            "Grade this quiz answer. Reply JSON: "
            '{"correct":bool,"score":0-100,"feedback":"one sentence"}\n\n'
            f"Question: {question}\nAnswer: {answer}"
        )
        try:
            return _heuristic_grade(prompt)
        except Exception as exc:  # noqa: BLE001
            return f"quiz grade error: {exc}"
    # generate — deterministic template quiz (no LLM dependency)
    if not topic:
        return "quiz generate needs a topic."
    qs = [
        f"1. Define {topic} in one sentence.",
        f"2. What is the most important property of {topic}?",
        f"3. Give a real-world example of {topic}.",
        f"4. How does {topic} relate to a closely related concept?",
        f"5. What would happen if {topic} didn't exist?",
    ][: max(1, min(num_questions, 10))]
    return "Quiz on " + topic + ":\n" + "\n".join(qs) + \
        "\n\nINSTRUCTION: Ask these one at a time. After each answer, call quiz(action='grade')."


# ---------------------------------------------------------------------------
# mastery — single tool covering the whole Guided Learning surface
# (upstream mastery_* family merged)
# ---------------------------------------------------------------------------


def _mastery_path() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "mastery.json"


@tool(mutating=True, tool_icon="🎯")
def mastery(action: str, topic: str = "", question: str = "",
            answer: str = "", score: int = -1, note: str = "") -> str:
    """Guided Learning mastery — one tool for the whole mastery loop.

    Actions:
    - ``topics``: list topics and their mastery state.
    - ``start``: begin a mastery session on ``topic``.
    - ``status``: current session state.
    - ``quiz``: get the next review question for ``topic``.
    - ``grade``: record learner's ``answer`` to ``question`` with ``score``
      (0-100, -1 = ungraded); updates mastery level.
    - ``note``: attach a free-form ``note`` to ``topic``.
    - ``leave``: end the current session.

    :param action: topics|start|status|quiz|grade|note|leave.
    :param topic: Topic name.
    :param question: Question being answered (grade).
    :param answer: Learner's answer (grade).
    :param score: 0-100 correctness (grade).
    :param note: Free-text note.
    """
    path = _mastery_path()
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        data = {"topics": {}, "session": None}
    topics: dict[str, Any] = data.setdefault("topics", {})
    session = data.get("session")

    action = (action or "status").lower()
    if action == "topics":
        if not topics:
            return "No mastery topics yet. Use mastery(action='start', topic='...')."
        return "Mastery topics:\n" + "\n".join(
            f"- {k}: level {v.get('level', 0)}/5, {v.get('reviews', 0)} reviews"
            for k, v in topics.items())
    if action == "start":
        if not topic:
            return "mastery start needs a topic."
        topics.setdefault(topic, {"level": 0, "reviews": 0, "notes": []})
        data["session"] = {"topic": topic, "started_at": time.time(), "turn": 0}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return f"Started mastery session on {topic}. Ask a diagnostic question."
    if action == "status":
        return f"Session: {session} " if session else "No active session. " + \
            f"Topics: {len(topics)}"
    if action == "quiz":
        t = session and topics.get(session.get("topic"))
        if not t:
            return "No active session — mastery(action='start', topic='...') first."
        lvl = t.get("level", 0)
        prompts = [
            f"Explain {session['topic']} from scratch.",
            f"Give a non-trivial example of {session['topic']}.",
            f"What are the edge cases of {session['topic']}?",
            f"Compare {session['topic']} with a related concept.",
            f"Teach {session['topic']} to a beginner in 3 steps.",
        ]
        q = prompts[min(lvl, len(prompts) - 1)]
        return f"QUIZ: {q}\n(INSTRUCTION: ask the learner, then call mastery(action='grade').)"
    if action == "grade":
        if not session or not question:
            return "mastery grade needs an active session and question."
        t = topics.setdefault(session["topic"], {"level": 0, "reviews": 0, "notes": []})
        t["reviews"] = int(t.get("reviews", 0)) + 1
        if score >= 0:
            # level 0-5: correct answers raise, wrong lower (min 0)
            delta = 1 if score >= 70 else (-1 if score < 40 else 0)
            t["level"] = max(0, min(5, int(t.get("level", 0)) + delta))
        if note:
            t.setdefault("notes", []).append({"at": time.time(), "note": note})
        t["updated_at"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return (f"Recorded: level {t['level']}/5 after {t['reviews']} reviews "
                f"on {session['topic']}.")
    if action == "note":
        tname = topic or (session or {}).get("topic")
        t = topics.setdefault(tname or "general", {"level": 0, "reviews": 0, "notes": []})
        t.setdefault("notes", []).append({"at": time.time(), "note": note})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return "Note saved."
    if action == "leave":
        data["session"] = None
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return "Mastery session ended."
    return f"Unknown mastery action: {action}"


# ---------------------------------------------------------------------------
# video — render an HTML/Remotion composition into a real video file
# ---------------------------------------------------------------------------


@tool(mutating=True, tool_icon="🎬")
def video(action: str, title: str = "", html: str = "",
          script: str = "", composition: str = "") -> str:
    """Create a video from an HTML/Remotion composition — one tool for the flow.

    Actions:
    - ``scaffold``: write a Remotion project skeleton into the workspace
      ``videos/<slug>/`` and return its path.
    - ``render``: render composition ``composition`` (default ``Main``) to
      ``videos/<slug>/out.mp4`` using ``npx remotion render``. Requires the
      scaffold to exist. Returns the output path or error.
    - ``list``: list rendered videos in the workspace.

    :param action: scaffold|render|list.
    :param title: Video title (used for the slug).
    :param html: Optional HTML/TSX body for the composition.
    :param script: Optional narration script text.
    :param composition: Remotion composition id for render.
    """
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    vdir = root / "videos"
    vdir.mkdir(parents=True, exist_ok=True)

    def _slug() -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (title or "video").lower()).strip("-") or "video"
        return f"{s}-{uuid.uuid4().hex[:6]}"

    action = (action or "scaffold").lower()
    if action == "list":
        outs = sorted(vdir.glob("*/out.mp4")) + sorted(vdir.glob("*/*.mp4"))
        return "\n".join(str(p.relative_to(root)) for p in outs) or "No videos yet."
    if action == "scaffold":
        slug = _slug()
        proj = vdir / slug
        (proj / "src").mkdir(parents=True, exist_ok=True)
        body = html or (
            "import {AbsoluteFill} from 'remotion';\n"
            "export const Main = () => (\n"
            f"  <AbsoluteFill style={{{{background:'#0b1020',color:'#fff',"
            "display:'flex',alignItems:'center',justifyContent:'center',"
            "fontSize:64}}}>\n"
            f"    <div>{(title or 'Video').replace('<','&lt;')}</div>\n"
            "  </AbsoluteFill>\n);\n"
        )
        (proj / "src" / "Composition.tsx").write_text(body)
        (proj / "src" / "index.ts").write_text(
            "import {registerRoot} from 'remotion';\n"
            "import {Composition} from 'remotion';\n"
            "import {Main} from './Composition';\n"
            "registerRoot(() => <Composition id=\"Main\" component={Main} "
            "durationInFrames={150} fps={30} width={1920} height={1080} />);\n"
        )
        (proj / "package.json").write_text(json.dumps({
            "name": slug, "private": True,
            "dependencies": {"react": "^18.2.0", "react-dom": "^18.2.0",
                             "remotion": "^4.0.0"},
            "scripts": {"render": "remotion render src/index.ts Main out.mp4"},
        }, indent=2))
        return f"Scaffolded Remotion project at videos/{slug}/ — run video(action='render', title='{slug}') to produce out.mp4"
    if action == "render":
        slug = title or ""
        proj = vdir / slug
        if not (proj / "package.json").exists():
            # find most recent scaffold if title omitted
            cands = sorted(vdir.glob("*/package.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not cands:
                return "No video project — run video(action='scaffold') first."
            proj = cands[0].parent
            slug = proj.name
        comp = composition or "Main"
        out = proj / "out.mp4"
        cmd = ["npx", "--yes", "remotion", "render", "src/index.ts", comp, "out.mp4"]
        try:
            proc = subprocess.run(cmd, cwd=proj, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            return "npx/remotion not available — scaffold exists at videos/%s but render needs Node toolchain." % slug
        except subprocess.TimeoutExpired:
            return "render timed out after 600s."
        if proc.returncode != 0 or not out.exists():
            return f"render failed: {proc.stderr[-500:] or proc.stdout[-500:]}"
        return f"Video rendered: videos/{slug}/out.mp4 ({out.stat().st_size} bytes)"
    return f"Unknown video action: {action}"


# ---------------------------------------------------------------------------
# apply_patch — ported from vtx coding_agent/tools/apply_patch.py
# Atomic file edits: ops update/create/delete/move, validated fully before
# any byte is written. Replaces the simple write_file tool.
# ---------------------------------------------------------------------------

_APPLY_PATCH_OPS = (
    '{"op":"update","path":"a.py","old":"exact text","new":"replacement"} | '
    '{"op":"update","path":"a.py","edits":[{"old":"..","new":"..","replace_all":false}]} | '
    '{"op":"create","path":"b.txt","content":"..."} | '
    '{"op":"delete","path":"c.txt"} | '
    '{"op":"move","path":"a","new_path":"b"}'
)


def _patch_root() -> Path:
    root = Path(os.environ.get("DT_WORKSPACE_DIR", "./data/workspace")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _patch_resolve(root: Path, p: str) -> Path:
    cand = (root / (p or "").lstrip("/")).resolve()
    if cand != root and root not in cand.parents:
        raise ValueError(f"path escapes workspace: {p}")
    return cand


@tool(mutating=True, tool_icon="🩹")
def apply_patch(ops_json: str) -> str:
    """Apply file edits atomically — the ONLY tool for file modifications.

    Pass a JSON array of ops (see format below). Every op is validated
    against an in-memory view first; if ANY op fails, NOTHING is written.

    Ops:
    - ``{"op":"update","path":P,"old":O,"new":N}`` — replace first exact
      occurrence of O (must match whitespace) with N; add
      ``"replace_all":true`` for all occurrences.
    - ``{"op":"update","path":P,"edits":[{"old":O,"new":N,"replace_all":bool}]}``
      — several edits to one file.
    - ``{"op":"create","path":P,"content":C}`` — new file (fails if exists).
    - ``{"op":"delete","path":P}`` — remove file.
    - ``{"op":"move","path":P,"new_path":Q}`` — rename.

    :param ops_json: JSON array of ops, e.g. [{"op":"update","path":"a.py",
        "old":"x = 1","new":"x = 2"}]
    """
    try:
        ops = json.loads(ops_json)
    except Exception as exc:  # noqa: BLE001
        return f"apply_patch: invalid JSON ops: {exc}"
    if not isinstance(ops, list) or not ops:
        return "apply_patch: ops must be a non-empty JSON array."

    root = _patch_root()
    _DELETED = object()
    state: dict[Path, Any] = {}

    def current(path: Path) -> str | None:
        if path in state:
            v = state[path]
            return None if v is _DELETED else v
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc

    def exists(path: Path) -> bool:
        if path in state:
            return state[path] is not _DELETED
        return path.exists()

    def mark(path: Path, content: str | None) -> None:
        state[path] = _DELETED if content is None else content

    try:
        for idx, raw in enumerate(ops):
            if not isinstance(raw, dict):
                raise ValueError(f"op {idx + 1}: not an object")
            op = raw.get("op")
            path = _patch_resolve(root, str(raw.get("path") or ""))
            label = f"op {idx + 1} ({op} {raw.get('path')})"

            if op == "update":
                edits = raw.get("edits")
                if edits is None:
                    edits = [{"old": raw.get("old"), "new": raw.get("new"),
                              "replace_all": raw.get("replace_all", False)}]
                content = current(path)
                if content is None:
                    raise ValueError(f"{label}: file does not exist")
                new_content = content
                for j, e in enumerate(edits):
                    old, new = e.get("old"), e.get("new")
                    if old is None or new is None:
                        raise ValueError(f"{label} edit {j + 1}: old/new required")
                    if old == new:
                        raise ValueError(f"{label} edit {j + 1}: old == new")
                    count = new_content.count(old)
                    if count == 0:
                        raise ValueError(f"{label} edit {j + 1}: 'old' not found")
                    if count > 1 and not e.get("replace_all"):
                        raise ValueError(
                            f"{label} edit {j + 1}: 'old' matches {count} times "
                            "— set replace_all or make it unique")
                    new_content = (new_content.replace(old, new)
                                   if e.get("replace_all")
                                   else new_content.replace(old, new, 1))
                mark(path, new_content)
            elif op == "create":
                if exists(path):
                    raise ValueError(f"{label}: file already exists")
                content = raw.get("content")
                if content is None:
                    raise ValueError(f"{label}: 'content' required")
                mark(path, str(content))
            elif op == "delete":
                if not exists(path):
                    raise ValueError(f"{label}: file does not exist")
                mark(path, None)
            elif op == "move":
                new_path = raw.get("new_path")
                if not new_path:
                    raise ValueError(f"{label}: 'new_path' required")
                content = current(path)
                if content is None:
                    raise ValueError(f"{label}: file does not exist")
                dest = _patch_resolve(root, str(new_path))
                if dest.exists() and dest not in state:
                    raise ValueError(f"{label}: destination exists")
                state[dest] = state.get(path, content)
                mark(path, None)
            else:
                raise ValueError(f"{label}: unknown op {op!r}")
    except Exception as exc:  # noqa: BLE001
        return f"apply_patch failed (no changes written): {exc}"

    # All ops validated — apply atomically.
    applied: list[str] = []
    try:
        for path, value in state.items():
            if value is _DELETED:
                if path.exists():
                    if path.is_dir():
                        import shutil
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                applied.append(f"deleted {path.relative_to(root)}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
                applied.append(f"wrote {path.relative_to(root)} ({len(value)} chars)")
    except Exception as exc:  # noqa: BLE001
        return f"apply_patch write failed mid-apply: {exc}"
    return "apply_patch applied:\n" + "\n".join(applied)


# ---------------------------------------------------------------------------
# web — single tool for search + fetch (vtx WebTool semantics: one tool,
# mode='search' queries, mode='fetch' downloads a URL's text)
# ---------------------------------------------------------------------------


