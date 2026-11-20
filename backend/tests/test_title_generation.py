"""The chat title is generated *after* the turn, never inside it.

Awaiting the title call before `done` made every first turn wait on a
third-party endpoint — up to the HTTP timeout after the answer was already on
screen. These tests pin the contract callers depend on: the turn is not held,
the client still receives the title as a `session_meta` frame once it lands, and
the pump's `settled` signal always fires so no event stream is left hanging.
"""

import asyncio

from app.agent.runtime import AgentRuntime, TurnHandle
from app.config import Settings
from app.store.session_store import SessionStore


class _Store(SessionStore):
    """SessionStore with saves counted, so a failed title can be asserted to
    leave the record alone."""

    def __init__(self, tmp_path):
        super().__init__(Settings(data_dir=tmp_path / "data"))
        self.saves = 0

    def save(self, session):  # type: ignore[override]
        self.saves += 1
        return super().save(session)


def _runtime(tmp_path):
    store = _Store(tmp_path)
    return AgentRuntime(store._settings, store), store


def _handle():
    return TurnHandle(turn_id="turn_1", session_id="sess_1")


def _session():
    return {"id": "sess_1", "title": "Untitled", "title_auto": True, "turns": {}}


def test_title_runs_off_the_turn_and_renders_as_a_late_frame(tmp_path, monkeypatch):
    runtime, store = _runtime(tmp_path)
    handle = _handle()
    session = _session()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_generate(user_text, assistant_text="", *, settings=None):
        started.set()
        await release.wait()
        return "PN junction study notes"

    monkeypatch.setattr("app.services.title_gen.generate_title", fake_generate)

    async def scenario():
        runtime._spawn_title_update(
            handle, session, {"content": "make me notes on pn junction"}, "holes…"
        )
        # The turn is not waiting on the title endpoint.
        assert not handle.settled.is_set()
        assert session.get("title") == "Untitled"
        # The attempt is claimed up front, so a second turn cannot start a rival
        # call while this one is still in flight.
        assert session["title_auto"] is False
        await started.wait()
        release.set()
        await asyncio.wait_for(handle.settled.wait(), timeout=5)

    asyncio.run(scenario())

    assert session["title"] == "PN junction study notes"
    assert store.saves == 1
    frames = [e for e in handle.events if e["type"] == "session_meta"]
    assert [f["metadata"] for f in frames] == [{"title": "PN junction study notes"}]


def test_a_dead_title_endpoint_never_blocks_or_retries(tmp_path, monkeypatch):
    runtime, store = _runtime(tmp_path)
    handle = _handle()
    session = _session()

    async def broken(user_text, assistant_text="", *, settings=None):
        raise RuntimeError("title endpoint down")

    monkeypatch.setattr("app.services.title_gen.generate_title", broken)

    async def scenario():
        runtime._spawn_title_update(handle, session, {"content": "hi"}, "")
        await asyncio.wait_for(handle.settled.wait(), timeout=5)

    asyncio.run(scenario())

    # One shot per session: the placeholder stays and the attempt is not retried.
    assert session["title"] == "Untitled"
    assert session["title_auto"] is False
    assert store.saves == 0
    assert [e for e in handle.events if e["type"] == "session_meta"] == []


def test_no_title_work_leaves_the_pump_free_to_close(tmp_path):
    runtime, _store = _runtime(tmp_path)
    session = {"id": "sess_1", "title": "Named by the learner", "turns": {}}
    assert not session.get("title_auto")
    # Nothing was spawned, so the runtime must have signalled the pump itself:
    # this mirrors the branch in the turn epilogue.
    handle = _handle()
    handle.settled.set()
    assert handle.settled.is_set()
