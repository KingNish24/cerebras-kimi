"""Knowledge-base agent tools backed by ``KnowledgeBaseStore`` (BM25 retrieval).

Mirrors DeepTutor's ``rag`` / ``kb_files`` / ``read_source`` tools closely
enough for the model to cite sources; the ``knowledge_bases`` turn field
selects which KBs are in scope (empty = all).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .toolkit import tool

from ..config import Settings
from ..store.kb_store import KnowledgeBaseStore

_store: KnowledgeBaseStore | None = None


def _kb() -> KnowledgeBaseStore:
    global _store
    if _store is None:
        _store = KnowledgeBaseStore(Settings())
    return _store


def _scoped_kbs(command_kbs: list[str] | None) -> list[str]:
    all_kbs = [k["name"] for k in _kb().list()]
    if not all_kbs:
        return []
    if command_kbs:
        return [k for k in command_kbs if k in all_kbs]
    return all_kbs


@tool(mutating=False, tool_icon="📚")
def knowledge_base_search(action: str = "search", query: str = "",
                          knowledge_bases: str = "", top_k: int = 5,
                          kb_name: str = "", doc: str = "") -> str:
    """Search the user's knowledge bases — one tool for the whole KB surface.

    Actions:
    - ``search`` (default): retrieve relevant passages for ``query``.
      ``knowledge_bases`` scopes (comma-separated; empty = all).
    - ``files``: list documents in the knowledge bases.
    - ``read``: read the full indexed text of one document — needs
      ``kb_name`` and ``doc`` (filename from files action).

    :param action: search|files|read.
    :param query: Search query (search).
    :param knowledge_bases: Comma-separated KB names (search/files).
    :param top_k: Passages per KB (search).
    :param kb_name: KB name (read).
    :param doc: Document filename (read).
    """
    names = [n.strip() for n in knowledge_bases.split(",") if n.strip()] or None
    kbs = _scoped_kbs(names)
    action = (action or "search").lower()

    if action == "files":
        if not kbs:
            return "No knowledge bases configured."
        out = []
        for name in kbs:
            docs = _kb().list_documents(name)
            out.append(f"KB '{name}': " + (", ".join(d["name"] for d in docs) if docs else "(empty)"))
        return "\n".join(out)

    if action == "read":
        text = _kb().read_source(kb_name, doc)
        return text if text else f"No indexed content for {doc!r} in KB {kb_name!r}."

    if not query:
        return "knowledge_base_search needs a query (action='search')."
    if not kbs:
        return "No knowledge bases configured."
    out = []
    for name in kbs:
        hits = _kb().search(name, query, top_k=top_k)
        for h in hits:
            out.append(f"[KB: {name} | source: {h['doc']} | chunk: {h['id']}]\n{h['text']}")
    return "\n\n---\n\n".join(out) if out else "No relevant passages found."
