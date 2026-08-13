# Configuration

robotsix-chat is configured via a single JSON config file, loaded by
[`robotsix-config`](https://github.com/damien-robotsix/robotsix-config). There is no YAML cascade
and no env-var overlay — the only environment variable consumed for config is the file locator.

## Config ownership

Per the
[config-ownership standard](https://damien-robotsix.github.io/robotsix-standards/config-ownership/),
component-owned configuration (feature flags, intervals, model selection, limits, behaviour toggles
— every key listed in this document) belongs to the component and is edited through its **own**
surface:

- **Settings panel** — the browser chat UI (`⚙ Settings`) loads the full config via `GET /config`
  and persists changes via `PUT /config`. This is the canonical edit path for all component-owned
  keys.
- **Config file** — operators can also edit `config/config.json` directly (or
  `config/config.local.json` for local development) and restart the service.

> **Migration note:** These keys were previously editable through the central-deploy config UI.
> Editing them there is now **deprecated** — the deploy plane owns only its own concerns (image,
> tag, mounts, ports, secret/env injection, restart policy, `ROBOTSIX_CONFIG_FILE` pointer). Use the
> chat Settings panel or direct file edit for all component-owned configuration going forward.
> Secrets (API tokens, keys) are **never** exposed in the component config file or UI — they remain
> env-injected via the deploy plane.

## Config file

The JSON file lives at **`config/config.json`** by default. Its path is set by the
`ROBOTSIX_CONFIG_FILE` environment variable.

**Getting started (when you need credentials):**

```bash
cp config/config.json config/config.local.json
# Edit config/config.local.json — fill in secrets for the features you enable.
ROBOTSIX_CONFIG_FILE=config/config.local.json uv run robotsix-chat
```

- `config/config.json` is **committed** — the defaults template (config standard): it documents
  every field with its default value, and central-deploy merges operator edits into it at deploy
  time. Never put real credentials in it.
- `config/config.local.json` is **gitignored** — the place for local credentials.
- `config/config.schema.json` is **committed and CI-checked** — the CI pipeline regenerates it from
  the `Settings` pydantic model and fails on any drift, so the schema always reflects the live code.

## Local dev

- The app starts with the committed defaults (`config/config.json`) out of the box — non-secret
  features (server, knowledge, diagnostics) just work.
- Copy it to `config/config.local.json` and set `ROBOTSIX_CONFIG_FILE` when you need secrets (API
  keys, API tokens) or want to override defaults.

## Secrets

Fields of JSON Schema type `string` with `writeOnly: true` are treated as secrets (`SecretStr`).
They are never logged, never serialized in diagnostics or trace output, and are redacted in stack
traces.

Secret fields include:

- `llmio_api_key`
- `langfuse.projects.<project>.public_key`, `langfuse.projects.<project>.secret_key`
- `memory.llm.api_key`, `memory.embedding.api_key`
- `central_deploy.api_token`
- `mail.api_token`
- `direct_repo.github_app_private_key`, `direct_repo.board_api_token`
- `feedback.board_api_token`

## Settings reference

All fields and their defaults are listed in `config/config.json`. The sections below describe each
group.

______________________________________________________________________

### Top-level

| JSON key                    | Type                | Default                                               | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| --------------------------- | ------------------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `llmio_model_level`         | `integer`           | `3`                                                   | LLM capability level: `1` (cheapest), `2`, `3`, or `4` (best).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `llmio_api_key`             | `string` (secret)   | `""`                                                  | OpenRouter API key. Required for levels 1–2; ignored for 3–4.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `chat_model_level`          | `integer` or `null` | `null`                                                | Optional override of `llmio_model_level` for the main interactive chat agent. When `null` (default), the chat agent uses `llmio_model_level`. Set to a specific level (e.g. `4` for fable-5) to route chat turns to a different tier while other consumers (subsessions, autonomous, summary) still use `llmio_model_level` or their own overrides.                                                                                                                                                                                                                                                       |
| `summary_model_level`       | `integer`           | `1`                                                   | LLM capability level used to regenerate `POST /summary`'s structured extraction after each turn.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `agent_instruction`         | `string`            | (long default)                                        | System instruction for the agent. Governed by the code default in `src/robotsix_chat/config/settings.py` (currently v112). Intentionally absent from `config/config.json` — the code default is the single source of truth. Operators who need to override it can add `"agent_instruction"` to their local or deployed config file; doing so bypasses the code default entirely. The agent's reply style is governed separately by [`docs/prompt-style.md`](prompt-style.md) — that file is automatically injected into every system prompt build and is the single source of truth for reply formatting. |
| `max_images_per_message`    | `integer`           | `8`                                                   | Maximum images per chat message.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `max_image_bytes`           | `integer`           | `5242880`                                             | Maximum image size in bytes (5 MiB).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `allowed_image_media_types` | `array[string]`     | `["image/png","image/jpeg","image/gif","image/webp"]` | Allowed image MIME types.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `low_risk_actions`          | `array[string]`     | `[]`                                                  | Action names/descriptions the agent may perform without human confirmation. When non-empty, these actions are pre-authorized and the agent will execute them without asking. Operators can list abstract descriptions (e.g. `"prioritize tickets on the board"`, `"close a subsession that has reached a terminal state"`) — the agent matches its available tools against the list at runtime.                                                                                                                                                                                                           |

#### Activating `low_risk_actions`

`low_risk_actions` ships default-off (`[]`) — the agent gates every action behind a confirmation
prompt. To pre-authorize actions:

1. **Edit the deployed config** (`config/config.json` in production; `config/config.local.json` for
   local dev). Add one or more action descriptions to the `low_risk_actions` list. For example:

   ```json
   "low_risk_actions": [
     "prioritize tickets on the board",
     "close a subsession that has reached a terminal state"
   ]
   ```

2. **Restart the service** so the new config is loaded.

3. **Live-proof:** start a chat session and ask the agent to perform one of the listed actions. The
   agent should execute it without requesting human confirmation. Confirm the assembled system
   prompt (visible in Langfuse traces or debug logging) contains the "Pre-authorized low-risk
   actions:" block with each listed action.

4. **Post-deploy follow-up:** after the next deployment cycle, verify that the `low_risk_actions`
   config key is present and non-empty in the deployed config file. If agent confirmation prompts
   still appear for listed actions, check that the config file is writable and that
   `ROBOTSIX_CONFIG_FILE` points to the correct path.

### Server

| JSON key                       | Type            | Default          | Description                                                                                          |
| ------------------------------ | --------------- | ---------------- | ---------------------------------------------------------------------------------------------------- |
| `server_host`                  | `string`        | `"0.0.0.0"`      | Host the server binds to.                                                                            |
| `server_port`                  | `integer`       | `8000`           | Port the server listens on.                                                                          |
| `idle_timeout_minutes`         | `integer`       | `30`             | Minutes of inactivity before closing the connection.                                                 |
| `compaction_min_turns`         | `integer`       | `3`              | Minimum fresh (not yet summarized) turns before compaction triggers.                                 |
| `compaction_keep_recent_turns` | `integer`       | `2`              | Most recent turns left verbatim after compaction so pending proposals and exact identifiers survive. |
| `log_level`                    | `string`        | `"INFO"`         | Python logging level.                                                                                |
| `log_json_format`              | `boolean`       | `true`           | When `true`, log lines are structured JSON (structlog); `false` for human-readable console output.   |
| `cors_allow_origins`           | `array[string]` | `[]`             | Origins allowed to call `/chat` cross-origin.                                                        |
| `correlation_id_header`        | `string`        | `"X-Request-ID"` | Header name for request correlation ids.                                                             |

**Compaction strategy.** When a session has been idle past `idle_timeout_minutes`, the turns before
the most recent `compaction_keep_recent_turns` turns are folded into a summary; the recent turns
stay verbatim in the agent's replay. The keep window (default `2`) is deliberately small — just
enough to preserve the tail of the conversation where a proposed-but-unexecuted plan (with its
ticket/message uids, file paths, and per-item decisions) normally lives, without growing the
replayed context. `compaction_min_turns` (default `3`) gates summarisation so tiny or freshly
compacted conversations never churn the summary agent, and compaction only fires when there are
strictly more fresh turns than the keep window, so a conversation that would be fully preserved
verbatim anyway is never summarised.

### Langfuse (tracing)

The canonical component-standard credential block: the instance host plus every Langfuse project
this component traces to, keyed by **project name**.

| JSON key                                 | Type              | Default                        | Description                           |
| ---------------------------------------- | ----------------- | ------------------------------ | ------------------------------------- |
| `langfuse.host`                          | `string`          | `"https://cloud.langfuse.com"` | Langfuse instance base URL.           |
| `langfuse.projects.<project>.public_key` | `string` (secret) | `""`                           | Langfuse public key for that project. |
| `langfuse.projects.<project>.secret_key` | `string` (secret) | `""`                           | Langfuse secret key for that project. |
| `langfuse.projects.<project>.project_id` | `string`          | `""`                           | Optional Langfuse project id.         |

This component declares two projects, per the component standard's one-project-per-LLM-function
rule:

- `robotsix-chat` — the main chat agent.
- `robotsix-chat-cognee` — the cognee/LiteLLM memory pipeline, named by `memory.langfuse_project`.
  See [Memory](#memory-cognee).

```json
"langfuse": {
  "host": "https://langfuse.example.net",
  "projects": {
    "robotsix-chat": {
      "public_key": "pk-lf-...",
      "secret_key": "sk-lf-...",  // pragma: allowlist secret
      "project_id": ""
    },
    "robotsix-chat-cognee": {
      "public_key": "pk-lf-...",
      "secret_key": "sk-lf-...",  // pragma: allowlist secret
      "project_id": ""
    }
  }
}
```

Keeping every component's credentials in this one standard block is what lets central-deploy
enumerate them uniformly and hand them to the fleet consumers that need them (the chat trace proxy,
cost-monitor's reconciliation).

### Langfuse Inspect

Trace-inspection tool that lets the agent query recent Langfuse traces. Reuses the main `langfuse`
credentials (public key, secret key, host) for API authentication — no separate credential fields.
Disabled by default.

| JSON key                      | Type      | Default | Description                                                |
| ----------------------------- | --------- | ------- | ---------------------------------------------------------- |
| `langfuse_inspect.enabled`    | `boolean` | `false` | Master switch — enables the `inspect_langfuse_trace` tool. |
| `langfuse_inspect.max_traces` | `integer` | `5`     | Maximum number of traces returned per query.               |

### Memory (cognee)

Persistent, cross-conversation episodic memory via embedded cognee. Disabled by default.

| JSON key                                 | Type              | Default                          | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------------- | ----------------- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.enabled`                         | `boolean`         | `false`                          | Master switch. Requires cognee extras.                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `memory.background_recall_enabled`       | `boolean`         | `true`                           | When `true` (default), background agents (subsessions and the autonomous loop) may READ memory even when their write gate is off — they get recall plus the `search_memory` tool, but `remember` is a no-op. Recall is a retrieval-only lookup while cognify is a multi-minute LLM pipeline, so there is no reason to deny background agents the accumulated context just because they must not pay to write it back. Set `false` to restore the previous all-or-nothing behaviour. |
| `memory.subsession_enabled`              | `boolean`         | `false`                          | When `true`, subsession agents (task / periodic / user_chat workers) get full memory (recall + cognify). Default `false` — background agents run continuously and would otherwise accrue cognee cost 24/7.                                                                                                                                                                                                                                                                          |
| `memory.autonomous_enabled`              | `boolean`         | `false`                          | When `true`, the autonomous auto-continue agent gets full memory. Default `false` for the same cost reason as `subsession_enabled`. Independent toggle so the two background classes can be gated separately.                                                                                                                                                                                                                                                                       |
| `memory.data_dir`                        | `string`          | `"/data/cognee"`                 | Cognee store directory (keep on persistent volume).                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `memory.recall_search_type`              | `string`          | `"CHUNKS"`                       | Cognee `SearchType` for the automatic per-message recall. Pure retrieval, no LLM call, so every chat turn stays cheap and fast. Deep, LLM-mediated search is available on demand via `deep_recall_search_type` and the `search_memory` tool.                                                                                                                                                                                                                                        |
| `memory.recall_timeout_seconds`          | `number`          | `60.0`                           | Hard timeout (seconds) for a single automatic `recall` call. On expiry degrades to empty string — the agent proceeds without memory.                                                                                                                                                                                                                                                                                                                                                |
| `memory.recall_max_concurrency`          | `integer`         | `4`                              | Maximum number of parallel recall operations that may run inside cognee at once.                                                                                                                                                                                                                                                                                                                                                                                                    |
| `memory.deep_recall_search_type`         | `string`          | `"GRAPH_COMPLETION"`             | Cognee `SearchType` for the on-demand `search_memory` tool. The expensive, LLM-mediated graph search — paid only when the agent deliberately asks for it instead of on every turn.                                                                                                                                                                                                                                                                                                  |
| `memory.deep_recall_timeout_seconds`     | `number`          | `180.0`                          | Hard timeout (seconds) for one `search_memory` tool call. More generous than the automatic recall's — the tool is invoked deliberately, so waiting longer is acceptable where stalling every message was not.                                                                                                                                                                                                                                                                       |
| `memory.remember_timeout_seconds`        | `number`          | `900.0`                          | Hard timeout (seconds) for ONE `remember` attempt (cognify consolidation). Default 900 s — raised from 300 after consecutive timeouts: cognify is a multi-minute LLM pipeline contending with recall for cognee's stores.                                                                                                                                                                                                                                                           |
| `memory.remember_max_attempts`           | `integer`         | `3`                              | How many times a write is attempted before the exchange is parked in the backlog. Each attempt gets the full `remember_timeout_seconds`; failures back off exponentially from `remember_retry_backoff_seconds`.                                                                                                                                                                                                                                                                     |
| `memory.remember_retry_backoff_seconds`  | `number`          | `30.0`                           | Base delay (seconds) before retrying a failed write, doubling per attempt. Backoff matters more than the retry count — an immediate retry re-enters the same store contention that caused the failure.                                                                                                                                                                                                                                                                              |
| `memory.write_backlog_path`              | `string`          | `"/data/cognee/backlog.jsonl"`   | Path to a durable JSONL backlog for exchanges that could not be persisted after retries are exhausted. Drained opportunistically on subsequent successful writes.                                                                                                                                                                                                                                                                                                                   |
| `memory.datafusion_runtime_memory_limit` | `string`          | `"256M"`                         | DataFusion memory-pool limit (e.g. `"256M"`, `"1G"`). Bounds the LanceDB worker subprocess memory so a single large `merge_insert` does not OOM the container. Safe default for 2 GB containers; raise for larger limits.                                                                                                                                                                                                                                                           |
| `memory.frozen_store_alert_minutes`      | `number`          | `10.0`                           | Duration (minutes) of consecutive write failures before a `WARNING` diagnostic is emitted — prevents a silently frozen vector store from going unnoticed for days.                                                                                                                                                                                                                                                                                                                  |
| `memory.auto_recovery_enabled`           | `boolean`         | `true`                           | When `true` (default), a freeze that persists past `frozen_store_recovery_minutes` triggers a guarded self-restart. Requires `lifecycle.enabled` and `lifecycle.service_name`.                                                                                                                                                                                                                                                                                                      |
| `memory.frozen_store_recovery_minutes`   | `number`          | `15.0`                           | Freeze duration (minutes) after which auto-recovery self-restart is attempted. Should exceed `frozen_store_alert_minutes` so the store is surfaced as degraded before restart.                                                                                                                                                                                                                                                                                                      |
| `memory.recovery_cooldown_minutes`       | `number`          | `30.0`                           | Minimum interval (minutes) between auto-recovery self-restart attempts — prevents restart loops when the store re-freezes immediately.                                                                                                                                                                                                                                                                                                                                              |
| `memory.write_throttle_seconds`          | `number`          | `0.5`                            | Delay (seconds) between serialised writes so the LanceDB worker subprocess can complete each `merge_insert` before the next starts. Prevents burst OOM.                                                                                                                                                                                                                                                                                                                             |
| `memory.llm.provider`                    | `string`          | `"custom"`                       | Extraction LLM provider.                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `memory.llm.model`                       | `string`          | `"openrouter/openai/gpt-5-nano"` | Extraction LLM model.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `memory.llm.endpoint`                    | `string`          | `"https://openrouter.ai/api/v1"` | Extraction LLM endpoint.                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `memory.llm.api_key`                     | `string` (secret) | `""`                             | OpenRouter API key for extraction.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `memory.llm.max_completion_tokens`       | `integer`         | `1024`                           | Maximum completion tokens per extraction LLM call. Caps output verbosity to control cost.                                                                                                                                                                                                                                                                                                                                                                                           |
| `memory.embedding.provider`              | `string`          | `"openai_compatible"`            | Embedding provider.                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `memory.embedding.model`                 | `string`          | `"bge-m3"`                       | Embedding model name.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `memory.embedding.endpoint`              | `string`          | `""`                             | Embedding server URL (e.g. `http://host:11434/v1`).                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `memory.embedding.dimensions`            | `integer`         | `1024`                           | Embedding vector dimensions.                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `memory.embedding.api_key`               | `string` (secret) | `""`                             | Bearer token for the embedding server.                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `memory.embedding.huggingface_tokenizer` | `string`          | `"BAAI/bge-m3"`                  | HuggingFace tokenizer name.                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `memory.langfuse_project`                | `string`          | `"robotsix-chat-cognee"`         | Name of the Langfuse project cognee's own LLM traffic traces to; its credentials are resolved from the top-level `langfuse` block.                                                                                                                                                                                                                                                                                                                                                  |

### Central Deploy

Component-access roster and skill loading from the central-deploy management plane.

| JSON key                                                        | Type              | Default  | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------------------------------------- | ----------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `central_deploy.url`                                            | `string`          | `""`     | Base URL of the central-deploy API (no trailing slash).                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `central_deploy.api_token`                                      | `string` (secret) | `""`     | Bearer token for the central-deploy API.                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `central_deploy.roster_cache_ttl`                               | `number`          | `300.0`  | Seconds to cache the component roster before re-fetching.                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `central_deploy.component_response_max_chars`                   | `integer`         | `200000` | Default truncation limit for GET/HEAD component responses — write methods keep the 8,000-char limit. Raised from 8,000 so large ticket lists (e.g. mill board blocked tickets) enumerate fully. Each call can override this with `component_request`'s `max_response_chars` parameter (e.g. `max_response_chars=2000` for a compact summary of a ticket history).                                                                                                            |
| `central_deploy.component_fallbacks`                            | `object`          | `{}`     | Baked-in fallback base URLs for components that may be missing from the central-deploy roster (e.g. after a redeploy). Keyed by component id (e.g. `"robotsix-mill"` → `"http://mill:8080"`). When the roster returned by central-deploy is missing a component, the fallback URL is used instead — keeps monitors and tool calls running through transient roster gaps. If a component is reported as unknown, the error message tells you exactly which config key to set. |
| `central_deploy.component_credentials.<id>.basic_auth_username` | `string` (secret) | `""`     | Username for HTTP Basic auth to component `<id>`.                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `central_deploy.component_credentials.<id>.basic_auth_password` | `string` (secret) | `""`     | Password for HTTP Basic auth to component `<id>`.                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `central_deploy.component_credentials.<id>.header_token`        | `string` (secret) | `""`     | Token for header-based auth (e.g. `X-API-Key`) to component `<id>`.                                                                                                                                                                                                                                                                                                                                                                                                          |

Keys are component IDs matching the central-deploy roster (the `id` field in
`GET /chat/components`). The roster entry's `auth.type` selects which credential fields are used —
only the fields matching the declared auth scheme are consulted when authenticating.

### Mail (board HTTP)

Direct HTTP access to the mill's board API for listing, reading, and creating tickets.

| JSON key            | Type              | Default                   | Description                                         |
| ------------------- | ----------------- | ------------------------- | --------------------------------------------------- |
| `mail.enabled`      | `boolean`         | `false`                   | Master switch.                                      |
| `mail.api_base_url` | `string`          | `"http://127.0.0.1:8077"` | Base URL of the board HTTP API (no trailing slash). |
| `mail.api_token`    | `string` (secret) | `""`                      | Optional bearer token for the board API.            |
| `mail.timeout`      | `number`          | `30.0`                    | Per-request HTTP timeout (seconds).                 |

### Conversation

| JSON key                         | Type      | Default                      | Description                                |
| -------------------------------- | --------- | ---------------------------- | ------------------------------------------ |
| `conversation.max_history_turns` | `integer` | `50`                         | Maximum conversation turns to retain.      |
| `conversation.max_conversations` | `integer` | `1000`                       | Maximum concurrent conversations.          |
| `conversation.persist_path`      | `string`  | `"/data/conversations.json"` | Path to the conversation persistence file. |

### Diagnostics

Failure capture and systemic fix surfacing. Enabled by default.

| JSON key                              | Type      | Default                                         | Description                                                      |
| ------------------------------------- | --------- | ----------------------------------------------- | ---------------------------------------------------------------- |
| `diagnostics.enabled`                 | `boolean` | `true`                                          | Master switch.                                                   |
| `diagnostics.store_path`              | `string`  | `"/data/diagnostics.json"`                      | Diagnostic-event JSON persistence path.                          |
| `diagnostics.proposals_path`          | `string`  | `"/data/fix_proposals.json"`                    | Fix-proposal JSON persistence path.                              |
| `diagnostics.effectiveness_path`      | `string`  | `"/data/diagnostics_effectiveness.json"`        | Effectiveness-report JSON persistence path.                      |
| `diagnostics.recurrence_threshold`    | `integer` | `3`                                             | Occurrences within the window to trigger a recurrence alert.     |
| `diagnostics.recurrence_window_days`  | `integer` | `30`                                            | Look-back window in days for recurrence detection.               |
| `diagnostics.observation_window_days` | `integer` | `30`                                            | Days after a fix to wait before an effectiveness report.         |
| `diagnostics.mill_events_path`        | `string`  | `"/data/robotsix-mill/diagnostic_events.jsonl"` | Path to the mill's JSONL event store for read_diagnostic_events. |

### Reference Docs (refdocs)

Read-only reference-docs tool — fetches documentation from allowlisted GitHub repos on demand.

| JSON key           | Type            | Default                    | Description                                |
| ------------------ | --------------- | -------------------------- | ------------------------------------------ |
| `refdocs.enabled`  | `boolean`       | `false`                    | Master switch. Requires non-empty `repos`. |
| `refdocs.repos`    | `array[string]` | `[]`                       | Allowlist of `owner/name` GitHub repos.    |
| `refdocs.ref`      | `string`        | `"main"`                   | Default git ref/branch to read from.       |
| `refdocs.base_url` | `string`        | `"https://api.github.com"` | Base URL for GitHub Enterprise.            |
| `refdocs.timeout`  | `number`        | `30.0`                     | Per-request HTTP timeout (seconds).        |

### Knowledge

Writable agent knowledge base — a plain JSON file on disk. Enabled by default.

| JSON key            | Type      | Default                  | Description                        |
| ------------------- | --------- | ------------------------ | ---------------------------------- |
| `knowledge.enabled` | `boolean` | `true`                   | Master switch.                     |
| `knowledge.path`    | `string`  | `"/data/knowledge.json"` | Path to the JSON persistence file. |

### Self-review

Read-only digest of live conversation activity. Disabled by default.

| JSON key                            | Type      | Default | Description                                              |
| ----------------------------------- | --------- | ------- | -------------------------------------------------------- |
| `self_review.enabled`               | `boolean` | `false` | Master switch — enables the `read_recent_activity` tool. |
| `self_review.recent_activity_limit` | `integer` | `20`    | Maximum conversations returned by the tool.              |

### Version Check

Self-version-check tool — compares the running version against the latest GitHub release. Disabled
by default.

| JSON key                  | Type      | Default                    | Description                                 |
| ------------------------- | --------- | -------------------------- | ------------------------------------------- |
| `version_check.enabled`   | `boolean` | `false`                    | Master switch.                              |
| `version_check.repo`      | `string`  | `""`                       | GitHub `owner/name`. Required when enabled. |
| `version_check.base_url`  | `string`  | `"https://api.github.com"` | Base URL for GitHub Enterprise.             |
| `version_check.timeout`   | `number`  | `30.0`                     | Per-request HTTP timeout (seconds).         |
| `version_check.cache_ttl` | `number`  | `300.0`                    | Seconds to cache the latest-release lookup. |

### Component Client

HTTP client for inspecting and configuring remote component agents. Disabled by default.

| JSON key                      | Type            | Default | Description                                                                             |
| ----------------------------- | --------------- | ------- | --------------------------------------------------------------------------------------- |
| `component_client.enabled`    | `boolean`       | `false` | Master switch.                                                                          |
| `component_client.timeout`    | `number`        | `240.0` | Per-request HTTP timeout (seconds).                                                     |
| `component_client.components` | `array[object]` | `[]`    | List of component targets, each with `base_url` (string) and optional `label` (string). |

### Subsessions

Background sub-agent spawning configuration.

| JSON key                                                | Type            | Default                    | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------- | --------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `subsessions.max_concurrent`                            | `integer`       | `8`                        | Maximum concurrent subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `subsessions.max_concurrent_per_session`                | `integer`       | `0`                        | Per-session cap on active subsessions. When a session reaches this limit, new spawns are rejected even if the global pool has room. Set to `0` to disable (no per-session limit).                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `subsessions.stale_reclaim_seconds`                     | `number`        | `0.0`                      | When the global pool is full but the spawning session is under its per-session limit, SLEEPING or PAUSED subsessions from other sessions that have been idle for longer than this many seconds are reclaimed (closed) to free a slot. SLEEPING subsessions are preferred over PAUSED because they count against the global capacity cap; reclaiming one actually frees a slot. Set to `0` to disable.                                                                                                                                                                                               |
| `subsessions.max_depth`                                 | `integer`       | `3`                        | Maximum nesting depth.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `subsessions.default_model_level`                       | `integer`       | `2`                        | Default model level for spawned subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `subsessions.min_interval_seconds`                      | `number`        | `60.0`                     | Minimum interval between periodic runs.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `subsessions.auto_stop_no_change_runs`                  | `integer`       | `3`                        | Consecutive NO_CHANGE runs before auto-stop.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `subsessions.max_idle_runs`                             | `integer`       | `15`                       | Consecutive NO_CHANGE runs before a periodic monitor auto-pauses (enters the real `paused` status). The monitor's worker stays alive and resumes automatically when the ticket state changes. Set to `0` to disable auto-pausing.                                                                                                                                                                                                                                                                                                                                                                   |
| `subsessions.run_timeout_seconds`                       | `number`        | `600.0`                    | Hard per-run timeout (seconds) for a single subsession turn. On expiry the run is marked failed and the schedule continues.                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `subsessions.store_path`                                | `string`        | `"/data/subsessions.json`" | Path to the subsession persistence file.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `subsessions.transcript_max_entries`                    | `integer`       | `200`                      | Maximum transcript entries per subsession.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `subsessions.human_approval_timeout_runs`               | `integer`       | `5`                        | When a periodic subsession's checkpoint indicates the monitored ticket is in `human_issue_approval` state, auto-escalate (close with reason `human_approval_timeout`) after this many consecutive `NO_CHANGE` runs.                                                                                                                                                                                                                                                                                                                                                                                 |
| `subsessions.human_approval_timeout_seconds`            | `number`        | `300.0`                    | Wall-clock backstop for the `human_issue_approval` stuck-ticket gate. When the checkpoint has carried `last_known_state='human_issue_approval'` for longer than this many seconds, auto-escalate even if the `NO_CHANGE` run count has not yet reached `human_approval_timeout_runs`. Default 300 (5 minutes).                                                                                                                                                                                                                                                                                      |
| `subsessions.pre_authorized_ticket_patterns`            | `array[string]` | `[]`                       | Glob patterns (`fnmatch`) matching ticket IDs that are pre-authorized under a standing operator directive. When a monitored ticket's ID matches a pattern, the `human_issue_approval` gate is bypassed — the system auto-escalates immediately (reason `pre_authorized_approval`) instead of waiting for `human_approval_timeout_runs`. Distinct from **user-requested tickets**, which are pre-authorized at filing in the same turn the operator asks the agent to file them (`kind: user-request` / `priority: high` markers; approved immediately out of draft) without any standing directive. |
| `subsessions.mill_recovery_initial_backoff_seconds`     | `number`        | `60.0`                     | Initial backoff (seconds) when a ticket monitor enters mill-recovery mode after consecutive failures. Doubles on each retry up to `mill_recovery_max_backoff_seconds`.                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `subsessions.mill_recovery_max_backoff_seconds`         | `number`        | `3600.0`                   | Maximum backoff (seconds) for mill-recovery retries (1 hour).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `subsessions.mill_recovery_max_retries`                 | `integer`       | `10`                       | Maximum number of recovery retries before the subsession is permanently closed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `subsessions.paused_monitor_poll_interval_seconds`      | `number`        | `60.0`                     | Interval (seconds) between polls of paused periodic monitors by the background watcher. The watcher checks each paused monitor's ticket state via the mill API; when the ticket's state differs from the checkpoint's `last_known_state` it sends an inbox wake message to the live paused worker (falling back to reopen+respawn if the worker is unreachable). Set to `0` to disable runtime polling (paused monitors only resume on service restart).                                                                                                                                            |
| `subsessions.paused_monitor_long_poll_interval_seconds` | `number`        | `15.0`                     | Interval (seconds) between direct mill API polls by a paused periodic monitor in its wait loop. Each paused monitor polls the mill for its tracked ticket's state at this interval; when the state differs from the checkpoint's `last_known_state` the monitor resumes immediately (zero added latency). The background watcher's `paused_monitor_poll_interval_seconds` serves as a safety-net backup. Set to `0` to disable per-monitor long-polling (watcher-only wake).                                                                                                                        |
| `subsessions.paused_monitor_auto_resume_seconds`        | `number`        | `1800.0`                   | Maximum wall-clock seconds a paused periodic monitor remains paused before auto-resuming regardless of ticket-state changes. When a monitor has been paused for longer than this interval (e.g. 1800 s = 30 min), it resumes its normal periodic cycle so the operator does not need to manually intervene. Set to `0` to disable time-based auto-resume (monitor stays paused until a state change or manual message arrives).                                                                                                                                                                     |
| `subsessions.paused_monitor_max_reblock_resumes`        | `integer`       | `3`                        | Maximum number of consecutive BLOCKED-on-resume events before a paused periodic monitor is closed with reason `repeated_blocked`. When a ticket is BLOCKED on every resume (the agent keeps hitting the same failure without making progress), auto-retry is futile — the monitor is closed so the operator can intervene. Set to `0` to disable auto-close on repeated blocks (the monitor will keep retrying indefinitely).                                                                                                                                                                       |
| `subsessions.paused_monitor_reblock_notify_threshold`   | `integer`       | `2`                        | Number of consecutive BLOCKED-on-resume events before an SSE notification is sent to the parent conversation alerting the operator that the monitor is re-blocking. This surfaces silent auto-resume→re-block loops so the operator can decide whether to rebase the branch, revert problematic files, or take other action before the `paused_monitor_max_reblock_resumes` cap is reached. Set to `0` to disable notifications.                                                                                                                                                                    |
| `subsessions.periodic_max_interval_seconds`             | `number`        | `3600.0`                   | Upper bound (seconds) for a periodic subsession's self-adjusted interval. The `adjust_periodic_interval` tool clamps to this value. Default 3600 (1 hour).                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `subsessions.periodic_max_total_runs`                   | `integer`       | `100`                      | Upper bound for a periodic subsession's self-adjusted `max_runs` (total run budget). The `adjust_periodic_budget` tool clamps to this value. Default 100.                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `subsessions.transient_error_max_retries`               | `integer`       | `3`                        | Maximum retry attempts when a periodic subsession's agent turn fails with a transient API error (e.g. OpenRouter upstream hiccup). Retries use exponential backoff between `transient_error_backoff_base` and `transient_error_backoff_cap`. When retries are exhausted the cycle is skipped and the schedule continues rather than permanently failing the subsession.                                                                                                                                                                                                                             |
| `subsessions.transient_error_backoff_base`              | `number`        | `1.0`                      | Initial backoff in seconds for transient-error retries (doubles each attempt).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `subsessions.transient_error_backoff_cap`               | `number`        | `30.0`                     | Maximum backoff in seconds for transient-error retries.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `subsessions.max_runs_escalation_threshold`             | `integer`       | `3`                        | Number of consecutive times a periodic subsession can hit its `max_runs` limit before auto-escalating with a follow-up ticket. When the threshold is reached, a follow-up ticket is created on the board and the monitor closes with reason `max_runs_escalated`. Set to `0` to disable escalation.                                                                                                                                                                                                                                                                                                 |
| `subsessions.user_chat_max_retries`                     | `integer`       | `3`                        | Maximum automatic retries for `user_chat` and `task` subsession failures. Each retry re-launches the subsession with the prior error folded into the prompt so the agent can self-correct. Once exhausted the subsession is failed and, for `user_chat`, the original decision prompt is surfaced in the main conversation as a fallback so the operator can answer directly.                                                                                                                                                                                                                       |

### Feedback

Automated feedback analysis for continuous self-improvement. When enabled, a feedback run analyses
the conversation at compaction and session-end boundaries, then files improvement tickets via the
board's `POST /tickets/ingest` endpoint. Tickets flow through the normal human-approval workflow —
the feedback run never auto-approves. Disabled by default.

| JSON key                        | Type              | Default | Description                                                                |
| ------------------------------- | ----------------- | ------- | -------------------------------------------------------------------------- |
| `feedback.enabled`              | `boolean`         | `false` | Master switch.                                                             |
| `feedback.model_level`          | `integer`         | `1`     | llmio capability level for the feedback-analysis agent (cheap extraction). |
| `feedback.board_url`            | `string`          | `""`    | Base URL of the board HTTP API (no trailing slash). Required when enabled. |
| `feedback.board_api_token`      | `string` (secret) | `""`    | Optional Bearer token for the board API.                                   |
| `feedback.deploy_api_key`       | `string` (secret) | `""`    | Bearer / X-API-Key token for the central-deploy roster endpoint.           |
| `feedback.timeout`              | `number`          | `60.0`  | Per-request HTTP timeout (seconds) for ingest calls.                       |
| `feedback.max_tickets_per_run`  | `integer`         | `3`     | Ceiling on tickets filed by one feedback run. `0` disables filing.         |
| `feedback.dedup_window_seconds` | `number`          | `60.0`  | Seconds to suppress duplicate runs (per session) and duplicate titles.     |

**Deduplication.** Two guards prevent near-simultaneous feedback runs from filing duplicate tickets:

1. **Session-level debounce** — when a feedback run is scheduled for a session that already had a
   run start within `dedup_window_seconds`, the new run is skipped. This prevents multiple
   compactions or session-end triggers (from concurrent subsessions) from spawning overlapping
   analyses of the same transcript.
2. **Title-level dedup** — before POSTing a ticket, the runner checks whether a ticket with the same
   normalised title (lowercased, stripped) was filed within `dedup_window_seconds`. This catches
   cross-session duplicates that the session-level debounce cannot guard.

Both caches are in-process only (no persistence), so a server restart resets them.

#### Observability (Langfuse traces)

Each feedback run produces a named Langfuse trace (`feedback-{trigger}`) tagged `feedback` and
`{trigger}`. The trace **root span** carries three ticket-count attributes:

| Attribute                 | Description                                 |
| ------------------------- | ------------------------------------------- |
| `feedback.total_tickets`  | Total tickets the runner attempted to file. |
| `feedback.filed_tickets`  | Tickets that received a 2xx response.       |
| `feedback.failed_tickets` | Tickets that received a non-2xx response or |
|                           | raised an HTTP exception.                   |

Individual `POST /tickets/ingest` spans set the OTel span status to `StatusCode.ERROR` on failure
(non-2xx or exception), include an `error.type` attribute (e.g. `http_503`), and call
`record_exception()` for HTTP exceptions — making filing failures immediately visible in Langfuse
without requiring log inspection. Span instrumentation errors are caught and never break the filing
loop.

#### Target repo resolution

Feedback tickets are filed against a set of allowed target repos. The set is resolved
**dynamically** at each feedback run (cached for 60 s) — there is no static `repo_ids` config key:

1. **Deploy roster** — `GET http://central-deploy:8100/chat/components` fetches the list of
   currently deployed chat components. Each component's `id` becomes a candidate target repo.
2. **Mill repo registry** — `GET http://mill:8077/repos` fetches the list of registered repos from
   the mill board.
3. **Intersection** — only repos present in *both* the deploy roster *and* the mill repo registry
   are allowed. A repo that is registered but not deployed (or vice versa) cannot receive tickets.
4. **Fallback** — if either service is unreachable, returns an empty response, or the intersection
   is empty, the runner falls back to `["robotsix-chat"]` and logs a warning so the feedback
   pipeline continues to function in a degraded state.

The `feedback.deploy_api_key` config field (see the table above) supplies the `X-API-Key` header for
the central-deploy API; it is needed only when the deploy server requires authentication. There is
no environment-variable equivalent — per the config standard's
[`environment:` rule](https://damien-robotsix.github.io/robotsix-standards/config-standard/#5-what-environment-is-for),
first-party credentials live in the config file and nowhere else.

### Direct Repo (GitHub App)

Push-branch and open-PR as the robotsix-mill GitHub App. Disabled by default.

| JSON key                                 | Type              | Default                    | Description                                                     |
| ---------------------------------------- | ----------------- | -------------------------- | --------------------------------------------------------------- |
| `direct_repo.enabled`                    | `boolean`         | `false`                    | Master switch.                                                  |
| `direct_repo.github_app_id`              | `string`          | `""`                       | GitHub App numeric or slug id. Required when enabled.           |
| `direct_repo.github_app_private_key`     | `string` (secret) | `""`                       | RSA private key in PEM format.                                  |
| `direct_repo.github_app_installation_id` | `string`          | `""`                       | Installation id to act as.                                      |
| `direct_repo.github_api_base_url`        | `string`          | `"https://api.github.com"` | Base URL for GitHub Enterprise.                                 |
| `direct_repo.board_api_base_url`         | `string`          | `"http://127.0.0.1:8077"`  | Board HTTP API base URL for ticket-state lookups.               |
| `direct_repo.board_api_token`            | `string` (secret) | `""`                       | Optional bearer token for the board API.                        |
| `direct_repo.timeout`                    | `number`          | `30.0`                     | Per-request HTTP timeout (seconds).                             |
| `direct_repo.direct_fix_enabled`         | `boolean`         | `false`                    | Enables the `direct_fix` branch-push tool (requires `enabled`). |

### GitHub Security

Repository security-feature toggle via the GitHub App installation. Disabled by default.

| JSON key                         | Type              | Default             | Description                                                              |
| -------------------------------- | ----------------- | ------------------- | ------------------------------------------------------------------------ |
| `github_security.enabled`        | `boolean`         | `false`             | Master switch.                                                           |
| `github_security.github_org`     | `string`          | `"damien-robotsix"` | GitHub organisation name whose repos are in scope.                       |
| `github_security.deploy_api_key` | `string` (secret) | `""`                | API key for the security-feature endpoint. Empty → endpoint returns 503. |

### GitHub Actions

GitHub Actions secrets and workflow dispatch via the GitHub App installation. Disabled by default.

| JSON key                        | Type              | Default             | Description                                                  |
| ------------------------------- | ----------------- | ------------------- | ------------------------------------------------------------ |
| `github_actions.enabled`        | `boolean`         | `false`             | Master switch.                                               |
| `github_actions.github_org`     | `string`          | `"damien-robotsix"` | GitHub organisation name whose repos are in scope.           |
| `github_actions.deploy_api_key` | `string` (secret) | `""`                | API key for Actions endpoints. Empty → endpoint returns 503. |

### Repo Study

Temporary local repo snapshots the agent can fetch (GitHub tarball — no `git` binary) and study with
read-only list/read/search tools. Workspaces expire after `ttl_minutes` and can be dropped early.
Authentication reuses the `direct_repo` GitHub App credentials when configured (the App's
installation scope defines the reachable private repos); public repos need no auth. Disabled by
default.

| JSON key                         | Type      | Default              | Description                                    |
| -------------------------------- | --------- | -------------------- | ---------------------------------------------- |
| `repo_study.enabled`             | `boolean` | `false`              | Master switch.                                 |
| `repo_study.data_dir`            | `string`  | `"/data/repo_study"` | Workspace directory (persistent volume).       |
| `repo_study.ttl_minutes`         | `integer` | `240`                | Workspace lifetime before the automatic sweep. |
| `repo_study.max_archive_bytes`   | `integer` | `67108864`           | Tarball download cap (64 MiB).                 |
| `repo_study.max_extracted_bytes` | `integer` | `268435456`          | Total uncompressed cap (256 MiB).              |
| `repo_study.max_read_bytes`      | `integer` | `204800`             | Per-read file byte cap.                        |
| `repo_study.timeout`             | `number`  | `60.0`               | Download HTTP timeout (seconds).               |

______________________________________________________________________

### Lifecycle

Deploy-lifecycle API client for inspecting and restarting the agent's own service. Disabled by
default.

| JSON key                              | Type              | Default  | Description                                                                                                                                             |
| ------------------------------------- | ----------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lifecycle.enabled`                   | `boolean`         | `false`  | Master switch.                                                                                                                                          |
| `lifecycle.base_url`                  | `string`          | `""`     | Base URL of the deploy-lifecycle API (no trailing slash). If the URL has no scheme (e.g. `central-deploy:8100`), `default_protocol` is prepended.       |
| `lifecycle.default_protocol`          | `string`          | `"http"` | Protocol scheme prepended when `base_url` lacks one (e.g. `"https"` for TLS). Ignored when `base_url` already has a recognised scheme (`http`/`https`). |
| `lifecycle.api_key`                   | `string` (secret) | `""`     | Optional API key for the deploy-lifecycle API.                                                                                                          |
| `lifecycle.service_name`              | `string`          | `""`     | Name of this service as registered with the deploy server.                                                                                              |
| `lifecycle.timeout`                   | `number`          | `30.0`   | Per-request HTTP timeout (seconds).                                                                                                                     |
| `lifecycle.self_restart_max_retries`  | `integer`         | `3`      | Maximum number of retries for transient `self_restart` failures (5xx, timeouts, connection errors). 0 = no retries.                                     |
| `lifecycle.self_restart_backoff_base` | `number`          | `1.0`    | Initial exponential-backoff delay in seconds. Doubled each retry: `base * 2^(attempt-1)`.                                                               |
| `lifecycle.self_restart_backoff_cap`  | `number`          | `30.0`   | Maximum exponential-backoff delay in seconds (ceiling). Retries never wait longer than this.                                                            |

______________________________________________________________________

### Notification

Browser notification settings — lets the agent alert the user proactively via the `notify_user`
tool. Enabled by default.

| JSON key               | Type      | Default | Description                                                    |
| ---------------------- | --------- | ------- | -------------------------------------------------------------- |
| `notification.enabled` | `boolean` | `true`  | Master switch. When `false`, no `notify_user` tool is offered. |

### HTTP Probe

Read-only HTTP uptime/render-probe tool for the agent. Enabled by default.

| JSON key                    | Type            | Default                                | Description                                                                  |
| --------------------------- | --------------- | -------------------------------------- | ---------------------------------------------------------------------------- |
| `http_probe.enabled`        | `boolean`       | `true`                                 | Master switch. When `false`, no `http_probe` tool is offered.                |
| `http_probe.timeout`        | `number`        | `10.0`                                 | Per-request HTTP timeout (seconds).                                          |
| `http_probe.allowlist`      | `array[string]` | `["www.robotsix.net", "robotsix.net"]` | Hostnames the tool is permitted to probe. Empty permits any public hostname. |
| `http_probe.max_body_bytes` | `integer`       | `2048`                                 | Maximum bytes of the response body to return (~2 KB).                        |
| `http_probe.max_redirects`  | `integer`       | `5`                                    | Maximum number of redirects to follow.                                       |

### Docker Digest

Read-only Docker Registry v2 digest-resolution tool for the agent. When enabled, the agent gains a
`resolve_docker_digest` tool that resolves a Docker image reference (e.g. `python:3.14-slim`) and
target platform to its immutable `sha256:...` content digest. Enabled by default.

| JSON key                      | Type      | Default                          | Description                                                                  |
| ----------------------------- | --------- | -------------------------------- | ---------------------------------------------------------------------------- |
| `docker_digest.enabled`       | `boolean` | `true`                           | Master switch. When `false`, no `resolve_docker_digest` tool is offered.     |
| `docker_digest.timeout`       | `number`  | `30.0`                           | Per-request HTTP timeout in seconds.                                         |
| `docker_digest.registry_host` | `string`  | `"registry-1.docker.io"`         | Docker Registry v2 hostname for manifest lookups (Docker Hub).               |
| `docker_digest.auth_url`      | `string`  | `"https://auth.docker.io/token"` | Token-authentication endpoint for bearer tokens (Docker Hub's auth service). |

### Gateway Route

Read-only gateway-route diagnostic tool for the agent. When enabled, the agent gains a
`check_gateway_route` tool that reads central-deploy's component registry, derives the current vhost
→ upstream mapping, and compares it with the expected `<slug>.<gateway_base_domain>` route for a
supplied service slug. Disabled by default.

| JSON key                            | Type      | Default                 | Description                                                                                                      |
| ----------------------------------- | --------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `gateway_route.enabled`             | `boolean` | `false`                 | Master switch. When `false`, no `check_gateway_route` tool is offered.                                           |
| `gateway_route.timeout`             | `number`  | `30.0`                  | Per-request HTTP timeout in seconds.                                                                             |
| `gateway_route.gateway_base_domain` | `string`  | `"deploy.robotsix.net"` | Fleet base domain used to derive the expected `<slug>.<base_domain>` vhost; must match central-deploy's setting. |

## Reaching fleet components

There is no credential setting. The tools that make outbound HTTP requests (`http_probe`,
`render_url`, `public_fetch`) reach fleet components at the internal `base_url` the central-deploy
roster already publishes for each one — `http://mail:8080`, not `https://mail.deploy.robotsix.net`.
Requests stay on the container network and never meet the fleet's edge or its SSO gate, so nothing
has to be provisioned, rotated, or kept out of logs.

The roster is the single place recording which components the agent may reach: a component appears
in it when its **chat access** toggle is enabled, and that is the whole configuration. Those hosts
are implicitly allowed through each tool's own host allowlist and SSRF check — an internal address
being exactly what a component's URL looks like.

A component without chat access enabled is not reachable. Its public `*.deploy.robotsix.net` URL is
not an alternative route: that lands on the SSO login page.

### Autonomous

Autonomous sessions are ordinary chat sessions that start automatically from their configured
trigger and run their configured prompt to completion, closing on the completion marker. There is no
proposal/approval handshake — plan/approval behaviour, if any, comes from the session's own prompt.

Session presets in `autonomous.sessions` are the **sole enablement model** — a preset that exists
and is enabled IS the enablement. There is no separate master switch. An explicit empty `sessions`
list is migrated to the built-in default preset on load; disable autonomous sessions by setting the
default preset's `enabled` to `false`.

| JSON key                                          | Type      | Default                       | Description                                                                                                                                                                                                                         |
| ------------------------------------------------- | --------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `autonomous.completion_marker`                    | `string`  | `"---AUTONOMOUS COMPLETE---"` | Marker string the agent emits when the run is complete. The session closes automatically on completion.                                                                                                                             |
| `autonomous.continue_interval_seconds`            | `number`  | `45.0`                        | Minimum pacing interval (seconds) between auto-continue loop iterations.                                                                                                                                                            |
| `autonomous.max_idle_auto_turns`                  | `integer` | `5`                           | Maximum number of consecutive NO_CHANGE / idle auto-continue turns before the loop halts (session closes). Set to `0` to disable the idle cap and only rely on per-preset `max_auto_turns`.                                         |
| `autonomous.stale_monitor_runs_before_completion` | `integer` | `3`                           | Number of consecutive `NO_CHANGE` cycles after which a periodic monitor is considered "stale" — the agent may declare the autonomous session complete even while the monitor is still running. Monitors continue in the background. |
| `autonomous.sessions`                             | `array`   | `[]`                          | List of named autonomous session definitions (see below). An explicit empty list is migrated to the built-in default preset on load.                                                                                                |

Each entry in `autonomous.sessions` is an `AutonomousSessionDefinition` object:

| JSON key                       | Type      | Default      | Description                                                                                                                                                              |
| ------------------------------ | --------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `name`                         | `string`  | *(required)* | Unique identifier for this session definition.                                                                                                                           |
| `prompt`                       | `string`  | `""`         | Custom kickoff prompt. When empty, the standard "Begin a new autonomous session and work it to completion" prompt is used.                                               |
| `trigger_type`                 | `string`  | `"periodic"` | Restart strategy: `"periodic"` (wait `trigger_interval_seconds`) or `"on_close"` (continuous mode).                                                                      |
| `trigger_interval_seconds`     | `number`  | `45.0`       | Delay between completion and restart for `"periodic"` trigger. Ignored for `"on_close"`.                                                                                 |
| `max_auto_turns`               | `integer` | `20`         | Maximum automatic agent turns during the run before the session closes.                                                                                                  |
| `enabled`                      | `boolean` | `true`       | When `false`, the definition is skipped — no session is created for it.                                                                                                  |
| `self_refine`                  | `boolean` | `false`      | When `true`, after each run completes an LLM refinement step proposes an updated prompt addendum that folds in the run's feedback. The next run uses the refined prompt. |
| `self_refine_require_approval` | `boolean` | `false`      | When `true`, refinements enter `pending` state and require operator approval before they take effect. When `false`, refinements are auto-accepted.                       |

**Named sessions.** Each entry in `autonomous.sessions` enables one autonomous session. Each
definition maps to a distinct pseudo-owner (`autonomous:<name>`, or `autonomous` for the `"default"`
preset), so sessions cannot overlap with themselves (the per-owner dedup invariant applies). Session
runs are logged and auditable — each run records the definition name, trigger reason, start/end
time, and summary.

**API.** The management surface is served at:

- `GET /autonomous/definitions` — list all definitions with their current active session.
- `POST /autonomous/definitions/{name}/run` — manually trigger a one-shot run (returns 409 if a
  session is already active).

**Example** — two autonomous sessions, one periodic and one continuous:

```json
"autonomous": {
  "sessions": [
    {
      "name": "default",
      "prompt": "",
      "trigger_type": "periodic",
      "trigger_interval_seconds": 45.0,
      "max_auto_turns": 20,
      "enabled": true
    },
    {
      "name": "continuous-triage",
      "prompt": "Begin an autonomous triage session.  Scan open tickets and investigate the oldest unassigned item.",
      "trigger_type": "on_close",
      "max_auto_turns": 30,
      "enabled": true
    }
  ]
}
```

______________________________________________________________________

### Autonomy

Operator-configurable autonomy tier that reduces interruptions for low-risk, mechanical decisions.
The default is conservative — every action is gated, so behaviour only changes when the operator
explicitly opts in.

Even at the highest tier these actions remain **hard-gated**: merges touching
`.github/workflows/**`, `secrets/**`, `.env*`, or any security-sensitive path; deletions of tracked
files or directories; priority/scope changes with broad blast radius; ambiguous or novel mutation
types; and any action whose safety the agent cannot independently verify.

| JSON key                                            | Type      | Default | Description                                                                                                                                                                                                                                                                                                                                                                                      |
| --------------------------------------------------- | --------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `autonomy.auto_approve_self_authored`               | `boolean` | `false` | When `true`, the agent may auto-approve `human_issue_approval` tickets it (or a chat-agent feedback source) authored, provided the target repo is in `auto_approve_repo_allowlist` and the change is non-destructive / reversible.                                                                                                                                                               |
| `autonomy.auto_approve_repo_allowlist`              | `array`   | `[]`    | Repository names (e.g. `"robotsix-chat"`) eligible for auto-approval when `auto_approve_self_authored` is enabled. Tickets targeting repos not listed here are always gated.                                                                                                                                                                                                                     |
| `autonomy.auto_approve_routine_secret_provisioning` | `boolean` | `false` | When `true`, the agent may auto-approve routine secret provisioning tickets even when they touch security-sensitive paths (`secrets/**`, `credentials`), provided the change has no code modifications, no destructive operations, and is limited to credential/secret/token provisioning. Covers standard operations like adding API keys, rotating credentials, or provisioning access tokens. |
| `autonomy.suppress_no_change_monitors`              | `boolean` | `false` | When `true`, periodic and event monitor outcomes that carry no actionable delta (NO_CHANGE, completed normally, auto-paused) do not generate an operator-facing turn. Only blockers and terminal failures are surfaced.                                                                                                                                                                          |
| `autonomy.auto_self_restart`                        | `boolean` | `false` | When `true`, the agent may call `self_restart` without operator approval after deploying capability changes (code changes, component roster updates) that affect the agent's own behaviour. The agent announces the restart with a brief delay so the operator can interrupt if needed.                                                                                                          |

**Example** — enabling auto-approval for self-authored tickets on the `robotsix-chat` repo, with
no-change monitor suppression, routine secret provisioning, and auto-self-restart:

```json
"autonomy": {
  "auto_approve_self_authored": true,
  "auto_approve_repo_allowlist": ["robotsix-chat"],
  "auto_approve_routine_secret_provisioning": true,
  "suppress_no_change_monitors": true,
  "auto_self_restart": true
}
```

______________________________________________________________________

### Render URL

Read-only URL rendering via headless Chromium (Playwright). Loads a URL in a headless browser,
captures a full-page screenshot (base64-encoded PNG), and extracts the accessibility tree — both
returned to the agent as structured JSON for UI verification. No interactive browsing, form-filling,
or navigation beyond the initial page load is permitted. Requires the `render-url` extra
(`playwright`) in the image as well as a Playwright Chromium browser installation. Disabled by
default.

| JSON key                     | Type      | Default | Description                              |
| ---------------------------- | --------- | ------- | ---------------------------------------- |
| `render_url.enabled`         | `boolean` | `true`  | Master switch.                           |
| `render_url.timeout`         | `number`  | `30.0`  | Per-request page-load timeout (seconds). |
| `render_url.viewport_width`  | `integer` | `1280`  | Browser viewport width (pixels).         |
| `render_url.viewport_height` | `integer` | `720`   | Browser viewport height (pixels).        |

______________________________________________________________________

### Public Fetch

Scoped public-repo-fetch tool for the chat agent. When enabled, the agent gains a `fetch_public_url`
tool that performs a plain HTTP(S) GET to a user-provided public URL, returns the raw text/file
contents with metadata, and writes an audit-log entry per fetch. SSRF protection blocks
internal/private IP ranges for public hosts; fleet components from the roster are reached at their
internal addresses and bypass that check, trusted by the operator and bypass the SSRF check.

| JSON key                                 | Type            | Default   | Description                                                                                                |
| ---------------------------------------- | --------------- | --------- | ---------------------------------------------------------------------------------------------------------- |
| `public_fetch.enabled`                   | `boolean`       | `false`   | Master switch. When `False`, no tool is offered.                                                           |
| `public_fetch.timeout`                   | `number`        | `10.0`    | Per-request HTTP timeout in seconds.                                                                       |
| `public_fetch.max_body_bytes`            | `integer`       | `1048576` | Maximum bytes of the response body to read and return to the agent (~1 MB).                                |
| `public_fetch.max_redirects`             | `integer`       | `5`       | Maximum number of redirects to follow.                                                                     |
| `public_fetch.domain_allowlist`          | `array[string]` | `[]`      | Optional list of hostnames (no protocol, no path) permitted for fetch. Empty = any public host is allowed. |
| `public_fetch.rate_limit_requests`       | `integer`       | `10`      | Maximum requests allowed within `rate_limit_window_seconds`.                                               |
| `public_fetch.rate_limit_window_seconds` | `number`        | `60.0`    | Sliding window in seconds for the rate limiter.                                                            |

### SFTP

SFTP config-restore capability. When enabled, the agent gains tools to read, list, and
(confirmation-gated) write files on a remote SFTP server — used to restore known-good configuration
files when diagnostics detect they are missing. Disabled by default.

| JSON key                      | Type              | Default | Description                                                                                                 |
| ----------------------------- | ----------------- | ------- | ----------------------------------------------------------------------------------------------------------- |
| `sftp.enabled`                | `boolean`         | `false` | Master switch. When `false`, no SFTP tools are registered and the agent runs exactly as before.             |
| `sftp.host`                   | `string`          | `""`    | SFTP server hostname or IP address.                                                                         |
| `sftp.port`                   | `integer`         | `22`    | SFTP server port.                                                                                           |
| `sftp.username`               | `string`          | `""`    | SFTP username for authentication.                                                                           |
| `sftp.password`               | `string` (secret) | `""`    | Password for password-based authentication. Leave empty when using key-based auth.                          |
| `sftp.private_key`            | `string` (secret) | `""`    | OpenSSH-format private key for key-based authentication. Leave empty when using password auth.              |
| `sftp.private_key_passphrase` | `string` (secret) | `""`    | Passphrase for `private_key`, if the key is encrypted.                                                      |
| `sftp.known_hosts`            | `string`          | `""`    | OpenSSH-format known-hosts entries for host key verification. When empty, host key verification is skipped. |
| `sftp.remote_root`            | `string`          | `""`    | Optional base directory on the remote server to restrict all operations under (e.g. `/var/www`).            |

## Schema

The committed `config/config.schema.json` is the authoritative schema for the `Settings` model. It
is auto-generated from the pydantic model via `Settings.model_json_schema()` and **CI-checked** to
stay in sync — a CI job regenerates it from the model and fails the build on any drift.

To regenerate locally:

```bash
python -c 'import json; from robotsix_chat.config import Settings; print(json.dumps(Settings.model_json_schema(), indent=2))' > config/config.schema.json
```
