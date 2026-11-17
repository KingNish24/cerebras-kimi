"""FastAPI entry point for the vtx-powered DeepTutor backend.

Run::

    uvicorn app.main:app --host 0.0.0.0 --port 8001

The Next.js frontend proxies ``/api/*`` and ``/ws`` here via
``web/proxy.ts`` (``DEEPTUTOR_API_BASE_URL``).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .agent.runtime import AgentRuntime
from .api import rest, rpc, ws
from .config import get_settings
from .store.session_store import SessionStore
from .store.kb_store import KnowledgeBaseStore

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)

    store = SessionStore(settings)
    runtime = AgentRuntime(settings, store)
    kb = KnowledgeBaseStore(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.store = store
        app.state.runtime = runtime
        app.state.kb = kb
        yield

    app = FastAPI(title="DeepTutor API (vtx)", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_origin_regex=r"https?://.*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(rest.router)
    app.include_router(ws.router)
    app.include_router(rpc.router)
    return app


app = create_app()
