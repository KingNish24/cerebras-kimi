# DeepTutor

An agent-native AI learning companion — Next.js frontend + Python backend powered by the [VTX coding-agent SDK](https://github.com/OEvortex/vtx-coding-agent). The frontend talks to the backend over **REST**; real-time turn and channel events are delivered over **Server-Sent Events (SSE)**. There are no WebSockets.

## Architecture

```
Browser ──POST /api/realtime/commands──▶ FastAPI (127.0.0.1:8001) ──▶ REST controller ──▶ AgentRuntime / vtx ConversationRuntime
Browser ◀──SSE /api/realtime/events───── FastAPI event queues ◀────────┘

Browser ──GET/POST /api/*──────────────▶ FastAPI handler registry (sessions, settings, KBs, notebooks, mastery, partners, …)
```

- **`backend/`** — FastAPI/Uvicorn app (`app/main.py`). REST handlers live in `app/api/handlers.py`; turn commands and feature-channel messages are dispatched directly by the REST+SSE controller in `app/api/realtime.py`, which drives the vtx agent loop in `app/agent/runtime.py`. There is no ACP or JSON-RPC layer.
- **`web/`** — Next.js app. `features/chat/transport/` holds the SSE-backed `TurnSocket` and the turn protocol client; `lib/sse-socket.ts` is the shared REST+SSE shim used by the turn protocol, `lib/rpc-ws.ts`, and `lib/reconnecting-websocket.ts` for legacy feature channels. Commands are POSTed; frames arrive as named SSE events and are re-emitted with the same JSON payloads the old transport produced.
- **DeepTutor owns sessions** — conversations persist as JSON documents under `data/sessions/<id>.json` (messages, turn events, settings, and the agent's vtx session header/entries). The agent uses a real, append-only vtx `Session`, but its persistence layer is that DeepTutor record; nothing is read from or written to `~/.vtx/sessions`.

### API surface

- **REST resources** (`/api/*`): auth, sessions and message events, settings and provider catalogs, knowledge bases and uploads, notebooks, question bank, mastery paths, partners and partner groups, documents, memory, dashboard, and more (`app/api/handlers.py`).
- **Turn commands** — `POST /api/realtime/commands` accepts the existing `ClientCommand` JSON: `start_turn`, `subscribe_turn`, `subscribe_session`, `resume_from`, `unsubscribe`, `cancel_turn`, `regenerate`, `submit_user_reply`, `user_input`, `check_active_turn`, `ping`.
- **Legacy channel messages** — the same endpoint with `?channel=<name>` (books, quiz judge, mastery paths, KB progress, partners, partner groups) wraps the payload as the backend's `legacy.send`.
- **Event stream** — `GET /api/realtime/events?client_id=<id>` (`text/event-stream`). Named events: `turn_event` (with `id: <turn_id>:<seq>` for replay), `session_update`, `legacy_event`, and `ready`/`heartbeat`. Each browser client has its own event queue, so events are scoped to the client that sent the command.
- **Client lifecycle** — `DELETE /api/realtime/clients/{client_id}` releases a client explicitly; idle clients are reclaimed by a periodic sweep.
- **Health** — `GET /health` → `{"status":"ok"}`.

## Backend layout (`backend/app/`)

| Path | Purpose |
|---|---|
| `main.py` | FastAPI app: middleware, state, REST router, SSE realtime router, `/health`, Uvicorn entry point |
| `api/realtime.py` | REST + SSE controller: per-client command dispatch (turns and feature channels), named-event stream, idle cleanup |
| `api/handlers.py` | REST handler registry mounted at `/api/*` |
| `agent/runtime.py` | `AgentRuntime` — builds vtx `ConversationRuntime`, owns turns/subscribers and event replay |
| `agent/tools.py` | Tool registry — `builtin_tools()` with toggleable/locked-on gating |
| `agent/dt_tools.py` | Consolidated tutor tools (see below) |
| `agent/kb_tools.py` | `knowledge_base_search` — BM25 retrieval over knowledge bases |
| `store/session_store.py` | DeepTutor session records — the sole conversation store |
| `agent/dt_session.py` | vtx `Session` subclass that persists entries into the DeepTutor record |
| `store/kb_store.py` | Knowledge-base store (chunked docs, BM25 search) |
| `config.py` | `Settings` — `data_dir`, `workspace_dir` via `DT_DATA_DIR`/`DT_WORKSPACE_DIR` |

## Agent tools (17 + codemode)

Consolidated single-purpose tools (vtx `WebTool`-style merges):

- **`web`** — Exa MCP search + Jina Reader fetch (`mode=search|fetch`)
- **`knowledge_base_search`** — `search|files|read` over uploaded KBs (BM25 + citations)
- **`memory`** — `read|write` durable learner facts (`data/memory.json`)
- **`notes`** — `write|list|search` markdown notebook (`data/notes/`)
- **`learning`** — `status|update|journal|weak` progress tracking
- **`mastery`** — Guided Learning loop `topics|start|status|quiz|grade|note|leave`
- **`workspace`** — `list|read|write|search|delete|mkdir|info` jailed file ops
- **`quiz`** — `generate|grade` (LLM grading via vtx runtime)
- **`video`** — Remotion pipeline `scaffold|render|list` → `workspace/videos/*.mp4`
- **`codemode`** — model-written Python in `vtx.codemode.CodemodeSandbox` calling other tools; `%bash <cmd>` magic runs shell commands
- **`apply_patch`** — vtx-style atomic file edits (`update|create|delete|move`, validate-then-write)
- **`ask_user`**, **`question_bank`**, **`paper_search`** (arXiv), **`read_file`/`list_files`**, **`list_tools`**

Model/provider config is shared with vtx: keys in `~/.vtx/dynamic_auth.json`, model caches in `~/.vtx/models/`, selection in `~/.vtx/config.yml` (`last_selected`/`recent_models`).

## Frontend layout (`web/`)

- `app/(workspace)/` — chat, settings, knowledge, calendar, notebook pages
- `proxy.ts` — rewrites `/api/*` to the backend; no WebSocket upgrade handling
- `app/files/[...path]/route.ts` — serves agent-generated files
- `lib/sse-socket.ts` — shared WebSocket-shaped client over REST command POST + SSE event stream
- `features/chat/transport/` — `TurnRuntimeClient`, `socket.ts` (SSE-backed), protocol parsing
- `lib/rpc-ws.ts`, `lib/reconnecting-websocket.ts` — legacy channel compatibility shims over the same SSE transport
- `lib/*-api.ts` — typed clients for sessions/settings/partners/notebooks/etc.

## Persistence

| Data | Location |
|---|---|
| Conversations | `data/sessions/<id>.json` (DeepTutor-owned) |
| DeepTutor session records | `data/sessions/` |
| Knowledge bases | `data/kb/` (via `KnowledgeBaseStore`) |
| Memory/learning/mastery/notes | `data/*.json`, `data/notes/` |
| Extended feature stores | `data/ext/` |
| Settings catalog/draft | `data/settings-catalog.json`, `data/settings-draft.json` |
| Workspace files | `data/workspace/` (`DT_WORKSPACE_DIR`) |
| Model/provider config | `~/.vtx/` (shared with vtx) |

## Development

The frontend and the Python backend are **separate processes**. The backend is a FastAPI/Uvicorn server on `127.0.0.1:8001` (REST under `/api/*`, SSE event stream at `/api/realtime/events`). `npm run dev` starts **only Next.js** — if the backend is not running, chat fails after ~12 s with *"Couldn't reach the server. Please check your connection and retry."*

```bash
# Terminal 1 — backend (required)
cd backend && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001

# Terminal 2 — frontend (http://localhost:3000)
cd web && npm run dev
```

Alternatively, `cd web && npm start` builds and starts both processes together.
Check the backend with `curl http://127.0.0.1:8001/health` → `{"status":"ok"}`.

## Notes

- `reff/` contains the upstream DeepTutor source used as reference (gitignored).
- Remaining feature gaps vs upstream: deep capability pipelines (`deep_research`, `mastery_path` stages), obsidian/ima/marginnote connectors — transports and tool surfaces exist, domain logic is incremental.
