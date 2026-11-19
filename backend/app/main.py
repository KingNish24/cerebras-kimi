"""FastAPI app — REST API + SSE transport (no WebSockets, no ACP).

REST handlers are mounted at /api/*. Turn commands and legacy feature-channel
messages are handled by the plain REST controller in ``app/api/realtime.py``,
which streams frames to the browser over ``GET /api/realtime/events``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .agent.runtime import AgentRuntime
from .api.handlers import router
from .api.realtime import RealtimeHub, router as realtime_router
from .config import get_settings
from .store.kb_store import KnowledgeBaseStore
from .store.session_store import SessionStore


def _use_deeptutor_vtx_sessions() -> None:
    """Back vtx Sessions with DeepTutor's store instead of ~/.vtx JSONL files.

    ConversationRuntime.initialize() creates a Session via Session.create().
    Redirecting that factory to DeepTutorSession keeps the full vtx session
    protocol (append-only, resumable, checkpoints) while persisting entries in
    ``data/sessions/<id>.json``. No vtx session files are read or written.
    """
    from vtx.agent.session import Session

    from .agent.dt_session import DeepTutorSession

    original_create = Session.__dict__["create"]

    def create(cls, cwd, *args, **kwargs):  # type: ignore[no-untyped-def]
        created = original_create.__func__(DeepTutorSession, cwd, *args, **kwargs)
        # Belt and braces: never let a base method fall back to JSONL I/O.
        created._session_file = None
        created._load_from_record()
        return created

    Session.create = classmethod(create)


_use_deeptutor_vtx_sessions()

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.workspace_dir.mkdir(parents=True, exist_ok=True)
settings.files_dir.mkdir(parents=True, exist_ok=True)

store = SessionStore(settings)
runtime = AgentRuntime(settings, store)
kb = KnowledgeBaseStore(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.realtime.start()
    try:
        yield
    finally:
        await app.state.realtime.stop()


app = FastAPI(title="DeepTutor", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.settings = settings
app.state.store = store
app.state.runtime = runtime
app.state.kb = kb
app.state.realtime = RealtimeHub(store=store, runtime=runtime)
app.include_router(router)
app.include_router(realtime_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8001, reload=False)


if __name__ == "__main__":
    main()
