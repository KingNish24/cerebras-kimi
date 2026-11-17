"""Stdio JSON-RPC server — the canonical ACP transport (ndJSON, one message
per line), same shape as ``vtx coding_agent/acp.py``.

A client (the Next.js bridge, an editor, or any ACP client) spawns::

    python -m app.acp_stdio

and speaks JSON-RPC 2.0 over stdin/stdout. No sockets, no HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import sys

from .acp import AcpAgent
from .config import get_settings
from .store.session_store import SessionStore
from .store.kb_store import KnowledgeBaseStore
from .agent.runtime import AgentRuntime


async def _main() -> None:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)

    store = SessionStore(settings)
    runtime = AgentRuntime(settings, store)
    kb = KnowledgeBaseStore(settings)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def send(msg: dict) -> None:
        # Single writer: stdout is ndJSON, one object per line.
        sys.stdout.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()

    from types import SimpleNamespace

    app_state = SimpleNamespace(settings=settings, store=store, runtime=runtime, kb=kb)
    agent = AcpAgent(store=store, runtime=runtime, app=SimpleNamespace(state=app_state), send=send)

    async def reader() -> None:
        # stdin is the only inbound channel; lines are JSON-RPC messages.
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break  # EOF: client closed stdin -> shutdown
            line = line.strip()
            if line:
                await queue.put(line)

    read_task = asyncio.create_task(reader())
    try:
        tasks: set[asyncio.Task] = set()
        while True:
            line = await queue.get()
            # Concurrent dispatch: a slow unary call (e.g. session/list
            # scanning ~/.vtx/sessions) must not starve turn.start/prompt.
            task = asyncio.create_task(agent.handle(line))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        read_task.cancel()
        await agent.close()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
