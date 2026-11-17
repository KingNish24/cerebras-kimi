"""ASGI entry point for the vtx-powered DeepTutor backend.

Run::

    uvicorn app.main:app --host 0.0.0.0 --port 8001

No web framework — the app is a plain ASGI callable (``app/asgi.py``)
speaking JSON-RPC 2.0 / ACP on ``/api/rpc`` and ``/api/rpc/ws``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from .agent.runtime import AgentRuntime
from .asgi import AcpApp
from .config import get_settings
from .store.session_store import SessionStore
from .store.kb_store import KnowledgeBaseStore

logging.basicConfig(level=logging.INFO)


def create_app() -> AcpApp:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)

    store = SessionStore(settings)
    runtime = AgentRuntime(settings, store)
    kb = KnowledgeBaseStore(settings)

    state = SimpleNamespace(
        settings=settings,
        store=store,
        runtime=runtime,
        kb=kb,
    )
    return AcpApp(state)


app = create_app()
