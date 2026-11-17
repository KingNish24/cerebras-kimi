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
from vtx.agent.sdk import Agent, Runner, tool

from ..config import Settings

_HEADERS = {"User-Agent": "DeepTutor/0.1 (+https://deeptutor.info)"}


# ---------------------------------------------------------------------------
# web_search — DuckDuckGo Lite HTML (no key required)
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="🔎")
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return titled results with URLs and snippets.

    :param query: Search query.
    :param max_results: Maximum results to return (default 5).
    """
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            resp = await client.post(
                "https://lite.duckduckgo.com/lite/",
                data={"q": query},
            )
            resp.raise_for_status()
        html = resp.text
        results = []
        # DDG lite: <a rel="nofollow" href="//duckduckgo.com/l/?uddg=...">title</a>
        for m in re.finditer(
            r'<a[^>]+href="([^"]*uddg=([^"&]+)[^"]*)"[^>]*>(.*?)</a>', html, re.S
        ):
            url = m.group(2)
            title = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            if title and url.startswith("http"):
                results.append((title, url))
            if len(results) >= max_results:
                break
        if not results:
            return "No results found."
        return "\n\n".join(f"{t}\n{u}" for t, u in results)
    except Exception as exc:  # noqa: BLE001
        return f"Error performing web search: {exc}"


# ---------------------------------------------------------------------------
# web_fetch — readable text extraction
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="📥")
async def web_fetch(url: str, max_chars: int = 20000) -> str:
    """Fetch a URL and return readable text (HTML stripped, scripts removed).

    :param url: HTTP(S) URL.
    :param max_chars: Maximum characters of text to return.
    """
    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=_HEADERS
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        text = resp.text
        text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?is)<!--.*?-->", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        import html as _html

        text = _html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text[:max_chars] or "(empty page)"
    except Exception as exc:  # noqa: BLE001
        return f"Error fetching {url}: {exc}"


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
# zotero_search — Zotero Web API (needs ZOTERO_API_KEY + ZOTERO_USER_ID)
# ---------------------------------------------------------------------------


@tool(mutating=False, tool_icon="📚")
async def zotero_search(query: str, max_results: int = 5) -> str:
    """Search the user's Zotero library.

    :param query: Search query.
    :param max_results: Maximum items to return.
    """
    key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not key or not user:
        return "Zotero is not configured (set ZOTERO_API_KEY and ZOTERO_USER_ID)."
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS) as client:
            resp = await client.get(
                f"https://api.zotero.org/users/{user}/items",
                params={"q": query, "limit": max_results, "format": "json"},
                headers={"Zotero-API-Key": key, **_HEADERS},
            )
            resp.raise_for_status()
        items = resp.json()
        out = []
        for it in items:
            data = it.get("data", {})
            title = data.get("title", "(untitled)")
            creators = data.get("creators", [])
            names = []
            for c in creators[:3]:
                names.append(c.get("name") or f"{c.get('firstName','')} {c.get('lastName','')}".strip())
            year = data.get("date", "")[:4]
            out.append(f"{title} — {', '.join(n for n in names if n)} ({year})\n{data.get('url','')}")
        return "\n\n".join(out) if out else "No Zotero items found."
    except Exception as exc:  # noqa: BLE001
        return f"Zotero search error: {exc}"


# ---------------------------------------------------------------------------
# LLM-backed tools: reason / brainstorm — tool-less sub-agents
# ---------------------------------------------------------------------------


def _subagent(model: str | None, instructions: str, name: str) -> Agent:
    return Agent(name=name, instructions=instructions, model=model, tools=[])


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


@tool(mutating=True, tool_icon="📝")
def write_note(title: str, content: str, tags: str = "") -> str:
    """Save a markdown note to the user's notebook.

    :param title: Note title.
    :param content: Markdown body.
    :param tags: Comma-separated tags.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "note"
    note_id = f"{slug}-{uuid.uuid4().hex[:6]}"
    path = _notes_dir_for_env() / f"{note_id}.md"
    meta = f"---\ntitle: {title}\ntags: {tags}\ncreated: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n---\n\n"
    path.write_text(meta + content, encoding="utf-8")
    return f"Saved note '{title}' as {note_id}"


def _notes_dir_for_env() -> Path:
    root = Path(os.environ.get("DT_DATA_DIR", "./data")).resolve() / "notes"
    root.mkdir(parents=True, exist_ok=True)
    return root


@tool(mutating=False, tool_icon="📓")
def list_notebook() -> str:
    """List all saved notes (id, title, tags)."""
    root = _notes_dir_for_env()
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


@tool(mutating=False, tool_icon="🔍")
def search_notes(query: str) -> str:
    """Full-text search across saved notes.

    :param query: Search terms.
    """
    root = _notes_dir_for_env()
    q = query.strip().lower()
    hits = []
    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if q in text.lower():
            idx = text.lower().find(q)
            snippet = text[max(0, idx - 80) : idx + 200].replace("\n", " ")
            hits.append(f"[{path.stem}] …{snippet}…")
    return "\n\n".join(hits[:10]) if hits else "No matching notes."


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


@tool(mutating=False, tool_icon="🧠")
def read_memory(query: str = "") -> str:
    """Read stored memories about the learner.

    :param query: Optional filter; empty returns everything.
    """
    path = _memory_path()
    if not path.exists():
        return "No memories stored yet."
    memories = json.loads(path.read_text(encoding="utf-8"))
    q = query.strip().lower()
    if q:
        memories = [m for m in memories if q in json.dumps(m, ensure_ascii=False).lower()]
    return json.dumps(memories[-50:], ensure_ascii=False, indent=1) or "[]"


@tool(mutating=True, tool_icon="💾")
def write_memory(fact: str, category: str = "general") -> str:
    """Store a durable fact about the learner for future sessions.

    :param fact: The fact to remember (one sentence).
    :param category: Optional category (e.g. 'preference', 'goal', 'background').
    """
    path = _memory_path()
    memories = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    memories.append({"fact": fact, "category": category, "created": time.time()})
    path.write_text(json.dumps(memories, ensure_ascii=False, indent=1))
    return f"Remembered: {fact}"


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
