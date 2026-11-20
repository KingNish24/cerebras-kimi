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
- **Scheduled reminders** — `GET/POST /api/cron/jobs`, `DELETE /api/cron/jobs/{id}`,
  `GET /api/cron/reports?since=` (fired reminders for live in-app delivery).
- **Reviews** — `GET /api/reviews/due`, `POST /api/reviews/{key}/grade` (spaced
  repetition over the question bank, 1/3/7/14/30/60-day boxes).
- **Calendar** — `GET/POST /api/calendar/calendars`, `PATCH/DELETE /api/calendar/calendars/{id}`,
  `GET/POST /api/calendar/events`, `PATCH/DELETE /api/calendar/events/{id}` (with
  `scope=this|following|all` + `occurrence`), `POST /api/calendar/undo/{token}`,
  `GET /api/calendar/search`, `GET /api/calendar/layers`, `GET /api/calendar/task-blocks`,
  `GET /api/calendar/export.ics`, and `GET /api/calendar/stream` (SSE revision feed).
  `GET /api/modules` reports `calendar` as installed so the sidebar entry, route gate and
  reminder runtime switch on.

## Backend layout (`backend/app/`)

| Path | Purpose |
|---|---|
| `main.py` | FastAPI app: middleware, state, REST router, SSE realtime router, `/health`, Uvicorn entry point |
| `api/realtime.py` | REST + SSE controller: per-client command dispatch (turns and feature channels), named-event stream, idle cleanup |
| `api/handlers.py` | REST handler registry mounted at `/api/*` |
| `api/calendar.py` | Calendar router at `/api/calendar` (calendars, events, RRULE expansion, search, ICS export, SSE revision stream, workspace layers) |
| `agent/runtime.py` | `AgentRuntime` — builds vtx `ConversationRuntime`, owns turns/subscribers and event replay |
| `agent/tools.py` | Tool registry — `builtin_tools()` with toggleable/locked-on gating |
| `agent/dt_tools.py` | Consolidated tutor tools (see below) |
| `agent/kb_tools.py` | `knowledge_base_search` — BM25 retrieval over knowledge bases |
| `store/session_store.py` | DeepTutor session records — the sole conversation store |
| `agent/dt_session.py` | vtx `Session` subclass that persists entries into the DeepTutor record |
| `store/kb_store.py` | Knowledge-base store (chunked docs, BM25 search) |
| `services/cron/` | Scheduled reminders/automations: `at`/`every`/`cron` jobs in a WAL SQLite store, scheduler loop, chat executor that runs a turn in the owning session |
| `services/calendar/` | Calendar domain: pydantic wire models, RFC 5545 recurrence engine (dateutil), SQLite store with undo, iCalendar writer, workspace layer projections |
| `config.py` | `Settings` — `data_dir`, `workspace_dir` via `DT_DATA_DIR`/`DT_WORKSPACE_DIR` |

## Agent tools

DeepTutor is a tutor, not a coding agent. vtx's default coding prompt, ponytail
mode, AGENTS.md/skills and git context are replaced by the tutor prompt in
`agent/prompt.py`. The exposed tool set is limited to:

- **`web`** — Exa MCP search + Jina Reader fetch (`mode=search|fetch`)
- **`paper_search`** — arXiv paper search
- **`knowledge_base_search`** — `search|files|read` over uploaded KBs (BM25 + citations)
- **`memory`** — `read|write` durable learner facts (`data/memory.json`)
- **`notes`** — `write|list|search` markdown notebook (`data/notes/`)
- **`learning`** — `status|update|journal|weak` progress tracking
- **`mastery`** — Guided Learning loop `topics|start|status|quiz|grade|note|leave`
- **`quiz`** — `generate|grade`
- **`video`** — Remotion pipeline `scaffold|render|list` → `workspace/videos/*.mp4`
- **`workspace`** — jailed ops for artifacts DeepTutor itself creates
- **`question_bank`** — stored question entries
- **`cron`** — `list|schedule|cancel` for scheduled reminders/automations (`at`, `every_seconds`, `cron_expr`); a due job runs a turn in the session it was scheduled from and appears on the calendar's Automations layer
- **`review`** — `due|grade|status` spaced repetition over the question bank; due cards show on the calendar's Reviews layer

Coding-agent tools (`apply_patch`, `read_file`, `list_files`, `codemode`,
`tool_search`, `ask_user`) are intentionally not registered.

### Provider / model configuration

DeepTutor owns its provider and model catalogue. Nothing is loaded from or featured out of `~/.vtx`: no `provider_catalog`, no `~/.vtx/models/` caches, no `~/.vtx/config.yml` (`last_selected`/`recent_models`) and no `~/.vtx/dynamic_auth.json`. The vtx SDK is used **only as an execution engine** — the backend passes the selected provider, model, base URL and API key into `ConversationRuntime` explicitly. (The engine still uses vtx's own built-in model-metadata tables internally for context windows; that metadata is not used to build the settings catalogue or choose a model.)

- **Built-in presets** — 62 providers (OpenAI, Anthropic, OpenRouter, DeepSeek, Zhipu, Together, Fireworks, NVIDIA, Hugging Face, Ollama, Aerolink, Kilo, WuS Router, aihubmix, Vercel AI, and more) live in `backend/app/provider_catalog.json` and are loaded by `backend/app/provider_config.py`. They supply connection prefills and fallback model options. Each entry carries its own `base_url`, `api_key_env`, model-discovery path and parser options.
- **User configuration** — connections (including API keys), profiles, models, the active `llm_selection` and `recent_models` — lives in `data/settings-catalog.json`. `data/settings.json` mirrors the active selection for older clients.
- **Model discovery** (`/api/settings/test-provider`, `/api/settings/fetch-models`) calls each preset's own `models_endpoint` directly (e.g. Experiential Labs uses `/api/models` with a custom parser) and stores the result in DeepTutor's catalog.
- **Provider icons** are bundled locally in `web/public/provider-icons/` (favicons fetched from the providers' domains, with a generated initials fallback), keyed by slug in `web/lib/provider-branding.ts`; provenance is recorded in `web/public/provider-icons/sources.json`. No external request is made to render them.
- A preset key can also come from its conventional env var (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) when no connection is configured.

## Frontend layout (`web/`)

- `app/(workspace)/` — chat, settings, knowledge, calendar, notebook pages
- `proxy.ts` — rewrites `/api/*` to the backend; no WebSocket upgrade handling
- `app/files/[...path]/route.ts` — serves agent-generated files
- `lib/sse-socket.ts` — shared WebSocket-shaped client over REST command POST + SSE event stream
- `features/chat/transport/` — `TurnRuntimeClient`, `socket.ts` (SSE-backed), protocol parsing
- `components/chat/home/ReminderButton.tsx` — composer "Remind me" control (one-off or daily/weekly at 09:00) posting to `/api/cron/jobs`
- `features/calendar/components/CalendarRuntime.tsx` — event reminders *and* polled cron reports: toast + desktop notification anywhere in the signed-in app
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
| Scheduled reminders | `data/cron/jobs.sqlite3` (job payloads, run history, next-run times); `data/cron/reports.json` (recent deliveries) |
| Review schedule | `data/reviews.json` (per-card box and due time; cards come from `data/question_bank.json`) |
| Calendars and events | `data/calendar/calendar.sqlite` (per-account calendars, recurring events, undo tokens) |
| Workspace files | `data/workspace/` (`DT_WORKSPACE_DIR`) |
| Model/provider config | `data/settings-catalog.json` (+ `data/settings.json` mirror); preset keys from env vars |

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
- `deep_question` (Quiz) is implemented: `agent/quiz.py` plans questions, generates them with the configured model, and emits `quiz_question_emitted` + `result` events for the interactive `QuizViewer`. AI Judge runs over the REST+SSE channel instead of a WebSocket.
- Remaining feature gaps vs upstream: deep capability pipelines (`deep_research`, `mastery_path` stages), obsidian/ima/marginnote connectors — transports and tool surfaces exist, domain logic is incremental.
