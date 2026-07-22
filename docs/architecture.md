# Architecture

## System Context

`robotsix-chat` is the **browser-facing chat server** in the robotsix fleet. It exposes an LLM agent
over HTTP (Starlette ASGI, SSE streaming) so human users converse with the agent through a
single-page browser UI.

```text
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
│  │ (Basic) │  │  (SSE)   │  │  (llmio, mail,    │  │
│  │         │  │          │  │   memory, …)       │  │
│  └─────────┘  └──────────┘  └─────────┬──────────┘  │
│                                       │              │
└───────────────────────────────────────┼──────────────┘
```

Some tools (`board_reader`, `knowledge`) are self-contained and operate locally.

The LLM itself is driven through `robotsix-llmio`, which abstracts the provider behind a
`model_level` (1–4). The agent never calls a provider directly. Background work runs in
**subsessions** — sub-agents spawned by the main agent (one-shot tasks, periodic monitors, or
user-facing side-chats), each at a model level picked by task difficulty.

______________________________________________________________________

## Start-Up Flow

The CLI entry point (`robotsix-chat`) executes `run_server_from_config()`:

```bash
uv run robotsix-chat
  │
  ├─ 1.  load_config(Settings)
  │      the one JSON config file (ROBOTSIX_CONFIG_FILE) over pydantic defaults
  │
  ├─ 2.  logging.config.dictConfig()
  │      correlation-ID-aware structured logging
  │
  ├─ 3.  _setup_observability()
  │      Langfuse + OpenTelemetry (graceful no-op when unavailable)
  │
  ├─ 4.  Build shared singletons
  │      ├─ EventBus              per-session SSE notification bus
  │      ├─ SubsessionRegistry    unified subsession lifecycle (persistent)
  │      ├─ ConversationStore     multi-session turn history (persistent)
  │      ├─ RunSerializer         per-owner asyncio lock
  │      └─ ParentDelivery        routes subsession summaries to their parent
  │
  ├─ 5.  create_agent_from_settings()
  │      Wires LlmioChatAgent with enabled tools:
  │      mail, memory, knowledge, refdocs,
  │      board_reader, selfreview, version_check, component_client
  │
  ├─ 6.  _resume()
         ├─ Creates Starlette app
         ├─ Registers routes + middleware
         └─ Stores singletons in app.state
```

______________________________________________________________________

## Request Lifecycle

### `POST /chat` — the main agent conversation endpoint

```text
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

The SSE stream delivers tokens as they arrive from the LLM. The browser renders them incrementally.
On `"done"` the client knows the reply is complete and can re-enable the input.

### Other Endpoints

| Method | Path                        | Purpose                                                |
| ------ | --------------------------- | ------------------------------------------------------ |
| `GET`  | `/`                         | Serve the chat UI (`ui/index.html`)                    |
| `GET`  | `/health`                   | Liveness probe (always open)                           |
| `GET`  | `/events?session_id=…`      | Persistent SSE channel for subsession lifecycle events |
| `GET`  | `/history?session_id=…`     | Retrieve stored conversation turns                     |
| `GET`  | `/subsessions?session_id=…` | List the session's subsession tree                     |
| `GET`  | `/subsessions/{id}`         | One subsession's snapshot + transcript                 |
| `POST` | `/subsessions/{id}/message` | Send a user message to a running subsession            |
| `POST` | `/subsessions/{id}/close`   | Close a subsession (summary still delivered)           |
| `POST` | `/summary`                  | Generate/refresh structured conversation summary       |
| `GET`  | `/sessions?owner_id=…`      | List all sessions for an owner                         |
| `POST` | `/sessions`                 | Create a new empty session                             |

______________________________________________________________________

## Autonomous Sessions

The autonomous subsystem lets the agent independently pick a subject, draft a plan, seek operator
approval, and execute the plan through tool calls — cycling back to subject selection after
completion. It was redesigned around a **single, continuous session** model to eliminate production
outages caused by blocking startup and accumulating sessions.

### Single-session model

When `autonomous.enabled=true`, there is **at most one open** autonomous session per owner at any
instant. "Open" means any non-terminal state (`selecting_subject`, `awaiting_approval`,
`executing`). Terminal states are `completed` (and any existing failed/cancelled terminal states).

- `create_session()` enforces this invariant: if the owner already has an open session, the existing
  session is returned unchanged and no new session is created.
- `_close_and_respawn()` checks the same invariant before spawning a successor, guarding against
  stale/duplicate sessions.

### Continuous respawn (complete → new session)

When the current open session reaches `completed`, a new autonomous session is automatically spawned
— the system maintains a continuous cycle while `autonomous.enabled=true`:

1. The `_auto_continue` loop detects the `completion_marker` in the agent's reply.
2. It schedules `_close_and_respawn` as a **detached background task** (never awaited).
3. `_close_and_respawn` removes the completed session, enforces the single-session invariant, and
   calls `create_session(…, schedule_kickoff=True)` which kicks off a fresh subject-selection →
   plan → approval → execution cycle.

The respawn is **idempotent**: after removing the completed session from the in-memory registry, a
concurrent duplicate trigger sees `None` and exits early.

### Non-blocking startup (never blocks chat)

All autonomous lifecycle work is moved off the startup/lifespan critical path:

| Operation | Where it runs | Blocking? |
| --- | --- | --- |
| Resume completed sessions | Background task via `_schedule_background` | Never |
| Resume executing sessions | Background task via `_schedule_background` | Never |
| Resume selecting-subject sessions | Background task via `_schedule_background` | Never |
| Close + respawn on completion | Background task via `_schedule_background` | Never |
| Initial turn kickoff | Background task via `_schedule_background` | Never |
| Auto-continue loop | Background task via `_schedule_background` | Never |

`resume_sessions()` (called from the lifespan) iterates persisted autonomous sessions and schedules
each one's handling as a background task, then returns immediately. Chat becomes available
regardless of whether the background tasks have finished or errored. Errors in background tasks are
caught and logged via `logger.exception`; they never propagate into the lifespan/startup path.

### Restart context message

When a session is resumed after a process restart, the agent receives a `"SYSTEM RESTARTED"` notice
in its prompt so it is aware it is resuming rather than starting cold:

- **`selecting_subject` sessions** — the restart notice is prepended to the initial-turn prompt
  (`_kickoff_initial_turn(…, is_restart=True)`).
- **`executing` sessions with `auto_turn_count == 0`** (first turn after approval) — the restart
  notice is prepended to the "OPERATOR APPROVAL RECEIVED" proceed message.
- **`executing` sessions with `auto_turn_count > 0`** (mid-execution) — the restart notice is
  prepended to the "Continue." message.
- **`completed` sessions** — handled by `_close_and_respawn` which spawns a fresh session with no
  restart notice needed (the new session starts cold).

### Session lifecycle

```text
  create_session()
        │
        ▼
  selecting_subject  ◄── reject
        │                 (reset after rejection)
        ▼
   _kickoff_initial_turn()
        │
        ▼
  awaiting_approval  ◄── max_auto_turns hit
        │
        ├─ approve() → executing
        └─ reject()  → selecting_subject (re-kickoff)
                          │
                          ▼
  executing ── _auto_continue() ──► awaiting_approval (blocker)
        │                                   │
        │                                   └─ approve() → executing (re-approval)
        │
        └─ completion_marker detected
                │
                ▼
           completed ──► _close_and_respawn() ──► create_session() (back to selecting_subject)
```

### Configuration

All autonomous behaviour is gated by the `autonomous.enabled` boolean config key (default `false`).
No new config keys were added for this redesign. See `docs/configuration.md` for the full autonomous
settings reference.

### UI changes

The "🤖 New autonomous" button previously shown in the sessions sidebar when `autonomous.enabled`
was `true` has been **removed**. With the single-session + continuous-respawn model, manual creation
is redundant and can violate the single-session invariant. The code path that checked
`GET /config` to conditionally show the button has also been removed from `chat.js`.

______________________________________________________________________

## Subpackage Inventory

Each subpackage lives under `src/robotsix_chat/`.

### Core

| Package            | Role                                                                                                                                                                                                                                                                                                                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`chat/`**        | Starlette app factory, route handlers, entry point. Conversation store (`conversation.py`) and SSE event bus (`events.py`).                                                                                                                                                                                                                                   |
| **`subsessions/`** | Unified subsession system — models, registry (state + inbox + persistence), worker turn loop, parent summary delivery (with dual-delivery for periodic-spawned decision chats: parent inbox + main-chat reaction), and the depth-aware agent tools (`spawn_subsession`, `message_subsession`, `close_subsession`, `list_subsessions`, `complete_subsession`). |
| **`llm/`**         | `LlmioChatAgent` — satisfies the `ChatAgent` protocol. Wraps `robotsix-llmio`'s `create_model(level)`, producing single-block (non-streamed) replies for claudeSDK transports.                                                                                                                                                                                |
| **`config/`**      | Pydantic `Settings` model (all configuration in one place), loaded from the one JSON config file via `robotsix-config` — no env overlay, no CLI merge. ~30 settings spanning LLM, server, memory, and all tool gates.                                                                                                                                         |
| **`ui/`**          | Single-file browser chat UI (`index.html`). No build step, no framework — served directly by `GET /`.                                                                                                                                                                                                                                                         |

### Optional Tools (gated by `settings.<tool>.enabled`)

| Package                 | Role                                                                                                         |
| ----------------------- | ------------------------------------------------------------------------------------------------------------ |
| **`mail/`**             | Email read/compose/send tools.                                                                               |
| **`knowledge/`**        | Durable knowledge-note tools (`add`/`append`/`update`/`list`/`read`). Process-local, no external dependency. |
| **`refdocs/`**          | `read_refdocs` tool — fetches documentation from allowlisted GitHub repositories.                            |
| **`repo_study/`**       | Temporary local repo snapshots (GitHub tarball, no git) the agent can list/read/search; TTL cleanup.         |
| **`selfreview/`**       | `read_recent_activity` tool — the agent can inspect its own conversation history to stay aware of context.   |
| **`version_check/`**    | Tools to check for newer package versions.                                                                   |
| **`component_client/`** | Tools to inspect and configure remote component agents over HTTP.                                            |

### Memory

| Package       | Role                                                                                                                                                                                                                                                                                |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`memory/`** | Optional long-term agent memory backed by **cognee**. When `memory.enabled` is set, the agent recalls relevant past context before each reply and persists new exchanges afterward (in a background task so latency is unaffected). Defaults to `NullMemory` (no-op) when disabled. |

______________________________________________________________________

## Configuration

All configuration flows through `robotsix_config.load_config(Settings)` — the
[config standard](https://damien-robotsix.github.io/robotsix-standards/config-standard/):

```text
pydantic field defaults  ←filled by←  the one JSON config file
```

- **Defaults**: sensible values in the pydantic model, mirrored by the committed template
  `config/config.json`.
- **The file**: located by the `ROBOTSIX_CONFIG_FILE` env var (default `config/config.json`) — the
  only source of values. No env-var overlay, no CLI merge.

The LLM provider is selected indirectly: `model_level` (1–4) is passed to `robotsix-llmio`, which
resolves it to a concrete provider (levels 3–4 → claudeSDK, levels 1–2 → OpenRouter DeepSeek). Level
4 (`claude-fable-5`) is the frontier tier and the default for the main chat agent; subsessions
default to level 3 unless the spawning agent picks otherwise.

______________________________________________________________________

## Long-Term Memory (cognee)

When enabled, the agent gains cross-conversation memory:

- **Recall**: before each reply, retrieves relevant past context.
- **Consolidation**: after replying, persists the exchange in the background (never adds latency).
- **Storage**: cognee data lives under `memory.data_dir` (default `/data/cognee`), bind-mounted for
  persistence across container redeploys.
- **Dependencies**: a remote embedding server (OpenAI-compatible, e.g. Ollama with `bge-m3`) and an
  extraction LLM (OpenRouter DeepSeek). Neither runs on the chat host.
- **Resilience features**:
  - **Write throttling** (`write_throttle_seconds`): a configurable delay between serialised writes
    prevents bursts of concurrent `merge_insert` calls from OOM-killing the LanceDB worker
    subprocess.
  - **Memory budget** (`datafusion_runtime_memory_limit`): the DataFusion memory pool is capped so a
    single large `merge_insert` cannot exhaust the container's memory.
  - **Durable backlog** (`write_backlog_path`): exchanges that fail after retries are written to a
    JSONL backlog and replayed opportunistically on the next successful write — no memories are
    silently dropped.
  - **Frozen-store detection** (`frozen_store_alert_minutes`): consecutive write failures lasting
    longer than the threshold emit a `WARNING` diagnostic so a silently frozen vector store cannot
    go unnoticed for days.

______________________________________________________________________

## Persistence

State that survives restarts when `/data/` is bind-mounted:

| File                               | Content                                                                                                                                    |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `/data/conversations.json`         | Multi-session conversation history (auto-migrated from legacy format)                                                                      |
| `/data/subsessions.json`           | Subsession state (periodic subsessions resumed on startup)                                                                                 |
| `/data/cognee/`                    | Long-term memory storage (cognee)                                                                                                          |
| `/data/autonomous_sessions.json`   | Autonomous session state (resumed on restart — see [Autonomous Sessions](#autonomous-sessions))                                            |

______________________________________________________________________

## Deployment

Two Docker Compose stacks:

- **Root `docker-compose.yml`** — local development: builds from the multi-stage `Dockerfile`,
  mounts `config/config.local.json` and `~/.claude` (for claudeSDK auth), binds port 8080.
- **`deploy/docker-compose.yml`** — production: the central-deploy contract (pre-built GHCR image,
  named volumes, config written by central-deploy into the `chat-config` volume). Lifecycle,
  networking, and authentication are handled by central-deploy and its gateway.

See `docs/getting-started.md` for setup instructions.
