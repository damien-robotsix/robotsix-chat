# Architecture

## System Context

`robotsix-chat` is the **browser-facing chat server** in the robotsix fleet.
It exposes an LLM agent over HTTP (Starlette ASGI, SSE streaming) so human
users converse with the agent through a single-page browser UI.

```
                  ┌────────────┐
                  │   Browser  │
                  │ (ui/index) │
                  └─────┬──────┘
                        │ HTTP (GET /, POST /chat, GET /health, …)
                        ▼
┌─────────────────────────────────────────────────────┐
│                 robotsix-chat                        │
│  ┌─────────┐  ┌──────────┐  ┌────────────────────┐  │
│  │  Auth   │  │  Routes  │  │  Agent + Tools     │  │
│  │ (Basic) │  │  (SSE)   │  │  (llmio, mill,     │  │
│  │         │  │          │  │   calendar, mail,   │  │
│  │         │  │          │  │   memory, …)        │  │
│  └─────────┘  └──────────┘  └─────────┬──────────┘  │
│                                       │              │
│                     ┌─────────────────┼──────────┐   │
│                     │  Agent-Comm Broker          │   │
│                     │  (robotsix-agent-comm)      │   │
│                     └─────────┬───────────────────┘   │
└───────────────────────────────┼───────────────────────┘
                                │
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │ robotsix-mill│  │robotsix-cal- │  │   robotsix-  │
     │ (board mgr)  │  │  endar       │  │  cost-monitor│
     └──────────────┘  └──────────────┘  └──────────────┘
```

The agent reaches other fleet services (mill, calendar, cost monitor) through
the **agent-comm broker** (`robotsix-agent-comm`) — a shared message bus that
routes JSON requests between agents. Tools like `consult_mill`, `query_calendar`,
and `component_client` all use this broker. Some tools (`board_reader`,
`knowledge`) are self-contained and operate locally.

The LLM itself is driven through `robotsix-llmio`, which abstracts the
provider behind a `model_level` (1–3). The agent never calls a provider
directly.

---

## Start-Up Flow

The CLI entry point (`robotsix-chat`) executes `run_server_from_config()`:

```
uv run robotsix-chat
  │
  ├─ 1.  Settings.load()
  │      pydantic defaults → config/chat.local.yaml → env vars
  │
  ├─ 2.  logging.config.dictConfig()
  │      correlation-ID-aware structured logging
  │
  ├─ 3.  _setup_observability()
  │      Langfuse + OpenTelemetry (graceful no-op when unavailable)
  │
  ├─ 4.  Build shared singletons
  │      ├─ EventBus              per-client SSE notification bus
  │      ├─ TaskRegistry          background sub-agent lifecycle
  │      ├─ CheckLoopRegistry     recurring check-loop lifecycle
  │      ├─ ConversationStore     multi-session turn history (persistent)
  │      ├─ RunSerializer         per-owner asyncio lock
  │      └─ ConversationDeliveryChannel  records task results into history
  │
  ├─ 5.  create_agent_from_settings()
  │      Wires LlmioChatAgent with enabled tools:
  │      mill, mail, calendar, memory, knowledge, refdocs,
  │      board_reader, selfreview, version_check, component_client
  │
  ├─ 6.  _resume()
  │      Restart persisted check loops (lifecycle hook)
  │
  ├─ 7.  (Optional) Start ComponentAgentResponder
  │      Broker listener for incoming agent-to-agent messages
  │
  └─ 8.  run_server() → create_app() → uvicorn.run(app, host, port)
         ├─ Creates Starlette app
         ├─ Registers routes + middleware
         └─ Stores singletons in app.state
```

---

## Request Lifecycle

### `POST /chat` — the main agent conversation endpoint

```
HTTP POST /chat  {"message": "...", "session_id": "...", "images": [...]}
  │
  ├─ CorrelationIdMiddleware     injects X-Request-ID into log context
  ├─ [optional] CORS / BasicAuth
  │
  └─ chat_endpoint(request)
       │
       ├─ 1. Parse + validate JSON body
       │
       ├─ 2. ConversationStore.begin(session_id)
       │      → (session_id, message_history)
       │
       └─ 3. Return StreamingResponse (text/event-stream)
              │
              ├─ Yield initial SSE heartbeat frame
              │
              ├─ Spawn producer task:
              │    ├─ Acquire per-owner RunSerializer lock
              │    ├─ agent.stream(message, history=..., session_id=..., images=...)
              │    ├─ Push tokens through asyncio.Queue
              │    ├─ On completion: store turn in ConversationStore
              │    └─ On error: yield error frame
              │
              └─ Consumer loop (waits on queue with 5 s timeout):
                   ├─ On token:   yield `data: {"type": "token", "content": "…"}`
                   ├─ On done:    yield `data: {"type": "done"}`, break
                   ├─ On timeout: yield SSE comment heartbeat
                   └─ On cancel:  clean up producer
```

The SSE stream delivers tokens as they arrive from the LLM. The browser
renders them incrementally. On `"done"` the client knows the reply is
complete and can re-enable the input.

### Other Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Serve the chat UI (`ui/index.html`) |
| `GET` | `/health` | Liveness probe (always open) |
| `GET` | `/events?session_id=…` | Persistent SSE channel for background-task lifecycle events |
| `GET` | `/history?session_id=…` | Retrieve stored conversation turns |
| `GET` | `/loops?session_id=…` | List active check loops |
| `POST` | `/loops/{id}/stop` | Stop a running check loop |
| `GET` | `/sessions?owner_id=…` | List all sessions for an owner |
| `POST` | `/sessions` | Create a new empty session |

---

## Subpackage Inventory

Each subpackage lives under `src/robotsix_chat/`.

### Core

| Package | Role |
|---------|------|
| **`chat/`** | Starlette app factory, route handlers, entry point, auth middleware (`auth.py`). Conversation store (`conversation.py`), SSE event bus (`events.py`), background-task lifecycle (`tasks.py`, `loops.py`, `runner.py`), and task-delegation tools (`delegation.py`). |
| **`llm/`** | `LlmioChatAgent` — satisfies the `ChatAgent` protocol. Wraps `robotsix-llmio`'s `create_model(level)`, producing single-block (non-streamed) replies for claudeSDK transports. |
| **`config.py`** | Pydantic `Settings` model (all configuration in one place). Cascade: field defaults → YAML (`config/chat.local.yaml`) → environment variables. ~30 settings spanning LLM, server, auth, memory, and all tool gates. |
| **`ui/`** | Single-file browser chat UI (`index.html`). No build step, no framework — served directly by `GET /`. |

### Optional Tools (gated by `settings.<tool>.enabled`)

| Package | Role |
|---------|------|
| **`mill/`** | `consult_mill` tool — forwards natural-language requests to the robotsix-mill board manager over the agent-comm broker. Disabled by default. |
| **`calendar/`** | `query_calendar` and `manage_calendar` tools — broker-based calendar and task operations via `robotsix-calendar`. |
| **`mail/`** | Email read/compose/send tools. |
| **`board_reader/`** | `list_board_tickets` and `read_board_ticket` — read-only board access via the same HTTP API the browser UI uses. Independent of the broker-based mill. |
| **`knowledge/`** | Durable knowledge-note tools (`add`/`append`/`update`/`list`/`read`). Process-local, no external dependency. |
| **`refdocs/`** | `read_refdocs` tool — fetches documentation from allowlisted GitHub repositories. |
| **`selfreview/`** | `read_recent_activity` tool — the agent can inspect its own conversation history to stay aware of context. |
| **`version_check/`** | Tools to check for newer package versions. |
| **`component_client/`** | Tools to send messages to other robotsix agents over the broker. |

### Agent-to-Agent Listener

| Package | Role |
|---------|------|
| **`component_agent/`** | Broker-based responder that listens for incoming agent-to-agent messages. Disabled by default; gated on `component_agent.enabled`. |

### Memory

| Package | Role |
|---------|------|
| **`memory/`** | Optional long-term agent memory backed by **cognee**. When `memory.enabled` is set, the agent recalls relevant past context before each reply and persists new exchanges afterward (in a background task so latency is unaffected). Defaults to `NullMemory` (no-op) when disabled. |

---

## Configuration Cascade

All configuration flows through `Settings.load()`:

```
pydantic field defaults  →  YAML (config/chat.local.yaml)  →  environment variables
```

- **Defaults**: sensible values in the pydantic model (e.g. `SERVER_PORT=8080`).
- **YAML**: `config/chat.local.yaml` is the primary operator-facing config file.
  A canonical template lives at `config/chat.local.example.yaml`.
- **Environment**: every setting has a corresponding env var (e.g. `SERVER_PORT`,
  `LLMIO_MODEL_LEVEL`, `AUTH_ENABLED`). Env vars override YAML, which overrides
  defaults.

The LLM provider is selected indirectly: `model_level` (1–3) is passed to
`robotsix-llmio`, which resolves it to a concrete provider (level 3 →
claudeSDK, levels 1–2 → OpenRouter DeepSeek).

---

## Long-Term Memory (cognee)

When enabled, the agent gains cross-conversation memory:

- **Recall**: before each reply, retrieves relevant past context.
- **Consolidation**: after replying, persists the exchange in the background
  (never adds latency).
- **Storage**: cognee data lives under `memory.data_dir` (default `.data/cognee`),
  bind-mounted for persistence across container redeploys.
- **Dependencies**: a remote embedding server (OpenAI-compatible, e.g. Ollama
  with `bge-m3`) and an extraction LLM (OpenRouter DeepSeek). Neither runs on
  the chat host.

---

## Persistence

State that survives restarts when `.data/` is bind-mounted:

| File | Content |
|------|---------|
| `.data/conversations.json` | Multi-session conversation history (auto-migrated from legacy format) |
| `.data/check_loops.json` | Running check-loop metadata (resumed on startup) |
| `.data/cognee/` | Long-term memory storage (cognee) |

---

## Deployment

Two Docker Compose stacks:

- **Root `docker-compose.yml`** — local development: builds from the multi-stage
  `Dockerfile`, mounts `config/chat.local.yaml` and `~/.claude` (for claudeSDK
  auth), binds port 8080.
- **`deploy/docker-compose.yml`** — production: pulls the GHCR image, binds
  loopback-only, adds a Watchtower sidecar for auto-redeploy, hardcodes
  `AUTH_ENABLED=1`.

See `docs/getting-started.md` for setup instructions.
