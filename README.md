# DeepTutor

An agent-native AI learning companion — Next.js frontend + Python backend powered by the [VTX coding-agent SDK](https://github.com/OEvortex/vtx-coding-agent). The entire stack speaks **JSON-RPC 2.0 / ACP** (Agent Client Protocol): no REST, no WebSocket, no FastAPI, no Uvicorn.

## Architecture

```
Browser ──POST /api/rpc──▶ Next.js route ──stdin──▶ python -m app.acp_stdio
Browser ◀──SSE /api/rpc/stream── Next.js ◀──stdout── (session/update, turn.event, legacy.event)
```

- **`backend/`** — pure stdio ACP agent (`app/acp_stdio.py` wraps the transport-agnostic `AcpAgent` core in `app/acp.py`). Reads ndJSON JSON-RPC on stdin, writes responses/notifications on stdout. Any ACP client can spawn it directly.
- **`web/`** — Next.js app. `lib/acp-bridge.ts` spawns the agent subprocess once (state on `globalThis`), `app/api/rpc/route.ts` forwards requests, `app/api/rpc/stream/route.ts` streams notifications over SSE.
- **Sessions are real vtx sessions** — conversations persist as JSONL under `~/.vtx/sessions/<cwd>/` via vtx's own `Session`/`JSONLSession`, shared with the vtx CLI/TUI (`session/list`, `session/load`, `session/new`, `session/prompt`, `session/cancel`, `session/set_model`…).

### ACP / RPC surface

- Standard ACP: `initialize`, `authenticate`, `session/new|load|list|delete|prompt|cancel|steer|rename|set_mode|set_model|set_config_option`
- Turn protocol: `turn.start|cancel|subscribe|subscribe_session|unsubscribe|regenerate|check_active|ping` → `turn.event` + `command_ack` notifications
- `http.request` — escape hatch dispatching ~130 internal handlers (settings, sessions, memory, notebooks, question bank, partners, skills, documents, mastery…) with the payloads the frontend expects. No HTTP routes exist for these.
- `legacy.send` — WS-compat bridge for book/quiz/mastery/progress streams (replies as `legacy.event` on named SSE channels)
- `quiz.judge`, `book.stream`, `progress.subscribe` — dedicated stream producers

## Backend layout (`backend/app/`)

| Path | Purpose |
|---|---|
| `acp.py` | Transport-agnostic ACP agent core — session lifecycle, turn pump, event mapping, RPC dispatch |
| `acp_stdio.py` | stdio ndJSON entry point (concurrent message dispatch) |
| `agent/runtime.py` | `AgentRuntime` — builds vtx `Agent` + `Runner`, owns turns/subscribers |
| `agent/tools.py` | Tool registry — `builtin_tools()` with toggleable/locked-on gating |
| `agent/dt_tools.py` | Consolidated tutor tools (see below) |
| `agent/kb_tools.py` | `knowledge_base_search` — BM25 retrieval over knowledge bases |
| `api/handlers.py` | ~130 internal request handlers (unmounted route registry) |
| `api/dispatch.py` | `http.request` dispatcher — path/query/body binding, no framework |
| `api/shims.py` | Minimal `Query`/`HTTPException`/`UploadFile`/`APIRouter` stand-ins |
| `store/session_store.py` | DeepTutor session records + `vtx_jsonl_session()` bridge to `~/.vtx/sessions` |
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
- **`quiz`** — `generate|grade` (LLM grading via vtx Runner)
- **`video`** — Remotion pipeline `scaffold|render|list` → `workspace/videos/*.mp4`
- **`codemode`** — model-written Python in `vtx.codemode.CodemodeSandbox` calling other tools; `%bash <cmd>` magic runs shell commands
- **`apply_patch`** — vtx-style atomic file edits (`update|create|delete|move`, validate-then-write)
- **`ask_user`**, **`question_bank`**, **`paper_search`** (arXiv), **`read_file`/`list_files`**, **`list_tools`**

Model/provider config is shared with vtx: keys in `~/.vtx/dynamic_auth.json`, model caches in `~/.vtx/models/`, selection in `~/.vtx/config.yml` (`last_selected`/`recent_models`).

## Frontend layout (`web/`)

- `app/(workspace)/` — chat, settings, knowledge, calendar, notebook pages
- `app/api/rpc/route.ts` + `app/api/rpc/stream/route.ts` — JSON-RPC POST + SSE
- `app/files/[...path]/route.ts` — serves agent-generated files
- `lib/acp-bridge.ts` — subprocess bridge + named-channel pub/sub
- `lib/rpc-ws.ts` — WebSocket-compatible `RpcSocket` over RPC+SSE (book/quiz/mastery/progress)
- `features/chat/transport/` — `TurnRuntimeClient`, `socket.ts` (fetch+EventSource), protocol parsing
- `lib/*-api.ts` — typed clients for sessions/settings/partners/notebooks/etc.

## Persistence

| Data | Location |
|---|---|
| Conversations | `~/.vtx/sessions/*/*.jsonl` (shared with vtx CLI) |
| DeepTutor session records | `data/sessions/` |
| Knowledge bases | `data/kb/` (via `KnowledgeBaseStore`) |
| Memory/learning/mastery/notes | `data/*.json`, `data/notes/` |
| Extended feature stores | `data/ext/` |
| Settings catalog/draft | `data/settings-catalog.json`, `data/settings-draft.json` |
| Workspace files | `data/workspace/` (`DT_WORKSPACE_DIR`) |
| Model/provider config | `~/.vtx/` (shared with vtx) |

## Development

```bash
# Backend agent (spawned automatically by Next.js)
cd backend && .venv/bin/python -m app.acp_stdio   # speaks ndJSON JSON-RPC on stdio

# Frontend
cd web && npm run dev    # http://localhost:3000
```

The Python side needs no server — the Next.js dev server spawns `python -m app.acp_stdio` as a child process. Direct stdio testing:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  | backend/.venv/bin/python -m app.acp_stdio
```

## Notes

- `reff/` contains the upstream DeepTutor source used as reference (gitignored).
- Remaining feature gaps vs upstream: deep capability pipelines (`deep_research`, `mastery_path` stages), obsidian/ima/marginnote connectors — transports and tool surfaces exist, domain logic is incremental.
