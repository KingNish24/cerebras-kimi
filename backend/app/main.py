"""FastAPI app — REST API + SSE transport (no WebSockets).

REST: the handler registry in ``app/api/handlers.py`` mounted at /api/*.
SSE: ``/api/realtime/events`` delivers turn/channel notifications and
``/api/realtime/commands`` accepts turn commands and legacy channel messages.
Both bridge into the same ``AcpAgent`` engine that used to sit behind stdio ACP.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .acp import AcpAgent  # noqa: F401 — kept importable for tooling/contracts
from .agent.runtime import AgentRuntime
from .api.handlers import router
from .api.realtime import RealtimeHub, router as realtime_router
from .config import get_settings
from .store.kb_store import KnowledgeBaseStore
from .store.session_store import SessionStore

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
app.state.realtime = RealtimeHub(
    store=store, runtime=runtime, app_state=SimpleNamespace(state=app.state)
)
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
