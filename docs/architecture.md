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

### Session ownership

The deployment is single-user: there is no login and no per-browser identity. `owner_id` is still
accepted on the wire (and still required, for backwards compatibility), but the server canonicalises
every client-supplied value to one operator pool — so the same session list is served to every
computer, browser, and private window. The only exception is the autonomous runner's reserved owners
(`autonomous` and `autonomous:<definition>`), which keep their own pool and are fetched by the UI as
a separate list.

______________________________________________________________________

## Autonomous Sessions

The autonomous subsystem lets the agent independently pick a subject, draft a plan including a
step-by-step proposal, present that proposal to the operator, and — when the operator comments on
the plan — execute it through tool calls. After execution the session stays open until the operator
explicitly closes it; there is no auto-close or respawn.

### Single-session model

When `autonomous.enabled=true`, there is **at most one open** autonomous session per owner at any
instant. "Open" means any non-terminal state (`planning`, `proposal`, `executing`). Terminal states
are `completed`.

- `create_session()` enforces this invariant: if the owner already has an open session, the existing
  session is returned unchanged and no new session is created.

### Lifecycle (spawn → proposal → execute → operator closes)

Sessions no longer auto-close or respawn. After completion the session stays open until the operator
manually closes it via the UI. A new session is only created when the operator explicitly starts
one.

### Non-blocking startup (never blocks chat)

All autonomous lifecycle work is moved off the startup/lifespan critical path:

| Operation                 | Where it runs                              | Blocking? |
| ------------------------- | ------------------------------------------ | --------- |
| Resume completed sessions | Left as-is (operator closes)               | Never     |
| Resume executing sessions | Background task via `_schedule_background` | Never     |
| Resume planning sessions  | Background task via `_schedule_background` | Never     |
| Resume proposal sessions  | Left for operator review                   | Never     |
| Initial turn kickoff      | Background task via `_schedule_background` | Never     |
| Auto-continue loop        | Background task via `_schedule_background` | Never     |

`resume_sessions()` (called from the lifespan) iterates persisted autonomous sessions and schedules
each one's handling as a background task, then returns immediately. Chat becomes available
regardless of whether the background tasks have finished or errored. Errors in background tasks are
caught and logged via `logger.exception`; they never propagate into the lifespan/startup path.

### Restart context message

When a session is resumed after a process restart, the agent receives a `"SYSTEM RESTARTED"` notice
in its prompt so it is aware it is resuming rather than starting cold:

- **`planning` sessions** — the restart notice is prepended to the initial-turn prompt
  (`_kickoff_initial_turn(…, is_restart=True)`).
- **`executing` sessions with `auto_turn_count == 0`** (first turn after proposal) — the restart
  notice is prepended to the "The operator has seen your plan" proceed message.
- **`executing` sessions with `auto_turn_count > 0`** (mid-execution) — the restart notice is
  prepended to the "Continue." message.
- **`proposal` sessions** — left for operator review; no restart notice needed.
- **`completed` sessions** — left as-is; the operator closes them when ready.

### Session lifecycle

```text
  create_session()
        │
        ▼
      planning  ──► _kickoff_initial_turn()
        │
        ▼
     proposal  ◄── max_auto_turns hit
        │
        └─ operator sends a message
                │
                ▼
           executing ── _auto_continue() ──► proposal (blocker)
                │
                └─ completion_marker detected
                        │
                        ▼
                   completed  ──► operator explicitly closes via UI
```

When the operator rejects a proposed subject, the plan text from that proposal is recorded in the
session's `rejected_subjects` list, persisted to the sessions JSON file, and injected into the
agent's prompt on the next subject-selection round as a "do not propose" instruction — preventing
the same subject from being re-picked until the session ends.

### Configuration

All autonomous behaviour is gated by the `autonomous.enabled` boolean config key (default `false`).
See `docs/configuration.md` for the full autonomous settings reference.

### UI changes

The "🤖 New autonomous" button previously shown in the sessions sidebar when `autonomous.enabled` was
`true` has been **removed**. With the single-session model, manual creation is redundant and can
violate the single-session invariant. The code path that checked `GET /config` to conditionally show
the button has also been removed from `chat.js`.

The Approve / Reject buttons that appeared when a session was awaiting approval have been removed.
Sessions in `proposal` state display "Awaiting review" with a plan snippet. The operator comments on
the plan in the chat to begin execution.

**Consent propagation.** When the operator authorises a complete operation (e.g. "use this password
and deploy this config change"), that consent carries forward automatically to all sub-operations in
the chain — ticket approval, MR approval, and merge — without the agent re-asking at intermediate
gates. Only genuinely new, unconsented actions that were not reasonably encompassed by the original
authorisation trigger a fresh approval request.

______________________________________________________________________

## Subpackage Inventory

Each subpackage lives under `src/robotsix_chat/`.

### Core

| Package            | Role                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`chat/`**        | Starlette app factory, route handlers, entry point. Conversation store (`conversation.py`) and SSE event bus (`events.py`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| **`subsessions/`** | Unified subsession system — models, registry (state + inbox + persistence), worker turn loop (with blocked-resume threshold detection: auto-closes after 3 consecutive blocked-on-resume events to prevent futile implement→blocked loops), retry logic (user_chat and task subsessions are retried on failure with the prior error folded into the prompt; retry state is persisted across restarts; exhausted retries for user_chat surface the decision prompt in the main conversation as a fallback), parent summary delivery (with dual-delivery for periodic-spawned decision chats: parent inbox + main-chat reaction), and the depth-aware agent tools (`spawn_subsession`, `message_subsession`, `close_subsession`, `list_subsessions`, `complete_subsession`)., and the paused-monitor background watcher (`watcher.py`) that polls mill ticket states and reopens paused periodic subsessions on state change. |
| **`llm/`**         | `LlmioChatAgent` — satisfies the `ChatAgent` protocol. Wraps `robotsix-llmio`'s `create_model(level)`, producing single-block (non-streamed) replies for claudeSDK transports.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| **`config/`**      | Pydantic `Settings` model (all configuration in one place), loaded from the one JSON config file via `robotsix-config` — no env overlay, no CLI merge. ~30 settings spanning LLM, server, memory, and all tool gates.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| **`ui/`**          | Single-file browser chat UI (`index.html`). No build step, no framework — served directly by `GET /`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

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

| File                             | Content                                                                                                                                                                                                                                                           |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/data/conversations.json`       | Multi-session conversation history (auto-migrated from legacy format)                                                                                                                                                                                             |
| `/data/subsessions.json`         | Subsession state (periodic subsessions resumed on startup; auto-closed monitors are re-spawned so the worker can re-verify ticket state — see `docs/periodic-checks.md`); retry counts for user_chat/task subsessions persisted so retry budget survives restarts |
| `/data/cognee/`                  | Long-term memory storage (cognee)                                                                                                                                                                                                                                 |
| `/data/autonomous_sessions.json` | Autonomous session state (resumed on restart — see [Autonomous Sessions](#autonomous-sessions))                                                                                                                                                                   |

______________________________________________________________________

## Deployment

Two Docker Compose stacks:

- **Root `docker-compose.yml`** — local development: builds from the multi-stage `Dockerfile`,
  mounts `config/config.local.json` and `~/.claude` (for claudeSDK auth), binds port 8080.
- **`deploy/docker-compose.yml`** — production: the central-deploy contract (pre-built GHCR image,
  named volumes, config written by central-deploy into the `chat-config` volume). Lifecycle,
  networking, and authentication are handled by central-deploy and its gateway.

See `docs/getting-started.md` for setup instructions.
