"""Dito is a temporary notebook pairing agent with its own tools and memory.

These tests pin the pieces that make Dito separate from the main chat:
its narrower toolset, its page-management tools, and the per-page recall of
earlier conversations.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent import dito
from app.agent.dito import DITO_SYSTEM_PROMPT
from app.agent.prompt import DEEPTUTOR_BASE
from app.agent.tools import builtin_tools
from app.agent.turn_context import set_note_context
from app.config import Settings
from app.services import dito_memory
from app.services.documents.storage import get_co_writer_storage


@pytest.fixture(autouse=True)
def reset_note_context():
    yield
    set_note_context("")


def _run(tool, **kwargs):
    return asyncio.run(tool.execute(tool.params(**kwargs)))


def _page(title: str = "PN Junction"):
    return get_co_writer_storage().create_document(
        title=title, content=f"# {title}\n\nIt has a depletion region."
    )


def test_dito_has_its_own_narrower_toolset(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_DATA_DIR", str(tmp_path))
    settings = Settings(data_dir=tmp_path)
    chat = {tool.name for tool in builtin_tools(settings)}
    assert {"video", "cron", "mastery", "workspace", "question_bank"} <= chat

    page = _page()
    set_note_context(page.id, generation_kind="")
    names = {tool.name for tool in builtin_tools(settings)}
    assert set(dito.DITO_OWN_TOOL_NAMES) <= names
    assert names == set(dito.DITO_TOOL_NAMES)
    assert not {"video", "cron", "mastery", "workspace", "question_bank"} & names


def test_note_create_and_list_manage_the_notebook(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_DATA_DIR", str(tmp_path))
    page = _page()
    set_note_context(page.id)

    created = _run(
        dito.note_create, title="MOSFET", content="# MOSFET\n\nA field-effect device."
    )
    assert created.success and "MOSFET" in created.result

    storage = get_co_writer_storage()
    child = next(doc for doc in storage.list_documents() if doc.title == "MOSFET")
    assert storage.load_document(child.id).parent_id is None

    sub = _run(dito.note_create, title="Channel", content="body", parent_here=True)
    assert sub.success
    nested = next(doc for doc in storage.list_documents() if doc.title == "Channel")
    assert storage.load_document(nested.id).parent_id == page.id

    listed = _run(dito.note_list, query="mos")
    assert "MOSFET" in listed.result and "PN Junction" not in listed.result

    found = _run(dito.note_search, query="depletion")
    assert "PN Junction" in found.result

    opened = _run(dito.note_open, page_id=nested.id)
    assert opened.success and "body" in opened.result


def test_dito_remembers_earlier_conversations_per_page(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_DATA_DIR", str(tmp_path))
    page = _page()
    set_note_context(page.id, generation_kind="")
    dito.remember_turn(
        page_id=page.id,
        session_id="session-one",
        user_text="Why does a depletion region form?",
        assistant_text="Carriers diffuse and recombine near the junction.",
        session_title="PN basics",
    )
    other = _page("MOSFET")
    dito.remember_turn(
        page_id=other.id,
        session_id="session-two",
        user_text="What is threshold voltage?",
        assistant_text="The gate voltage that inverts the channel.",
    )

    here = dito_memory.conversations(page_id=page.id)
    assert [item["session_id"] for item in here] == ["session-one"]
    rendered = dito_memory.render(here)
    assert "depletion region" in rendered and "MOSFET" not in rendered

    all_pages = dito_memory.conversations(query="threshold")
    assert [item["session_id"] for item in all_pages] == ["session-two"]

    recall = _run(dito.dito_memory, query="depletion")
    assert recall.success and "Carriers diffuse" in recall.result
    assert "threshold" not in recall.result

    set_note_context(page.id, generation_kind="")
    prompt = dito.notebook_context_block(page.id, "session-three", "", "")
    assert "Earlier conversations on this page" in prompt
    assert "Why does a depletion region form?" in prompt


def test_dito_memory_skips_the_current_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_DATA_DIR", str(tmp_path))
    page = _page()
    dito.remember_turn(
        page_id=page.id,
        session_id="current",
        user_text="earlier question",
        assistant_text="earlier answer",
    )
    prompt = dito.notebook_context_block(page.id, "current", "", "")
    assert "earlier question" not in prompt


def test_deleting_a_page_forgets_its_dito_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_DATA_DIR", str(tmp_path))
    page = _page()
    dito.remember_turn(page_id=page.id, session_id="s", user_text="q", assistant_text="a")
    assert dito_memory.conversations(page_id=page.id)

    get_co_writer_storage().delete_document(page.id)
    assert dito_memory.forget_page(page.id)
    assert not dito_memory.conversations(page_id=page.id)


def test_every_tool_named_in_a_prompt_is_registered(tmp_path, monkeypatch):
    import re

    monkeypatch.setenv("DT_DATA_DIR", str(tmp_path))
    settings = Settings(data_dir=tmp_path)

    def missing(prompt: str, available: set[str]) -> list[str]:
        named = set(re.findall(r"`([a-z_][a-z0-9_]*)`", prompt))
        return sorted(name for name in named if name not in available)

    chat = {tool.name for tool in builtin_tools(settings)}
    assert missing(DEEPTUTOR_BASE, chat) == []

    page = _page()
    set_note_context(page.id)
    dito_set = {tool.name for tool in builtin_tools(settings)}
    assert missing(DITO_SYSTEM_PROMPT, dito_set) == []
    assert dito_set == set(dito.DITO_TOOL_NAMES)
