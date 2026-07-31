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
- `refdocs.github_token`
- `version_check.github_token`
- `feedback.board_api_token`

## Settings reference

All fields and their defaults are listed in `config/config.json`. The sections below describe each
group.

______________________________________________________________________

### Top-level

| JSON key                    | Type              | Default                                               | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------- | ----------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `llmio_model_level`         | `integer`         | `3`                                                   | LLM capability level: `1` (cheapest), `2`, `3`, or `4` (best).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `llmio_api_key`             | `string` (secret) | `""`                                                  | OpenRouter API key. Required for levels 1–2; ignored for 3–4.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `summary_model_level`       | `integer`         | `1`                                                   | LLM capability level used to regenerate `POST /summary`'s structured extraction after each turn.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `agent_instruction`         | `string`          | (long default)                                        | System instruction for the agent. Governed by the code default in `src/robotsix_chat/config/settings.py` (currently v86). Intentionally absent from `config/config.json` — the code default is the single source of truth. Operators who need to override it can add `"agent_instruction"` to their local or deployed config file; doing so bypasses the code default entirely. The agent's reply style is governed separately by [`docs/prompt-style.md`](prompt-style.md) — that file is automatically injected into every system prompt build and is the single source of truth for reply formatting. |
| `max_images_per_message`    | `integer`         | `8`                                                   | Maximum images per chat message.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `max_image_bytes`           | `integer`         | `5242880`                                             | Maximum image size in bytes (5 MiB).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `allowed_image_media_types` | `array[string]`   | `["image/png","image/jpeg","image/gif","image/webp"]` | Allowed image MIME types.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `low_risk_actions`          | `array[string]`   | `[]`                                                  | Action names/descriptions the agent may perform without human confirmation. When non-empty, these actions are pre-authorized and the agent will execute them without asking. Operators can list abstract descriptions (e.g. `"prioritize tickets on the board"`, `"close a subsession that has reached a terminal state"`) — the agent matches its available tools against the list at runtime.                                                                                                                                                                                                          |

### Server

| JSON key                | Type            | Default          | Description                                                                                        |
| ----------------------- | --------------- | ---------------- | -------------------------------------------------------------------------------------------------- |
| `server_host`           | `string`        | `"0.0.0.0"`      | Host the server binds to.                                                                          |
| `server_port`           | `integer`       | `8000`           | Port the server listens on.                                                                        |
| `idle_timeout_minutes`  | `integer`       | `30`             | Minutes of inactivity before closing the connection.                                               |
| `log_level`             | `string`        | `"INFO"`         | Python logging level.                                                                              |
| `log_json_format`       | `boolean`       | `true`           | When `true`, log lines are structured JSON (structlog); `false` for human-readable console output. |
| `cors_allow_origins`    | `array[string]` | `[]`             | Origins allowed to call `/chat` cross-origin.                                                      |
| `correlation_id_header` | `string`        | `"X-Request-ID"` | Header name for request correlation ids.                                                           |

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
| `memory.llm.model`                       | `string`          | `"openrouter/openai/gpt-5-mini"` | Extraction LLM model.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `memory.llm.endpoint`                    | `string`          | `"https://openrouter.ai/api/v1"` | Extraction LLM endpoint.                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `memory.llm.api_key`                     | `string` (secret) | `""`                             | OpenRouter API key for extraction.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
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

| JSON key                              | Type      | Default                                  | Description                                                  |
| ------------------------------------- | --------- | ---------------------------------------- | ------------------------------------------------------------ |
| `diagnostics.enabled`                 | `boolean` | `true`                                   | Master switch.                                               |
| `diagnostics.store_path`              | `string`  | `"/data/diagnostics.json"`               | Diagnostic-event JSON persistence path.                      |
| `diagnostics.proposals_path`          | `string`  | `"/data/fix_proposals.json"`             | Fix-proposal JSON persistence path.                          |
| `diagnostics.effectiveness_path`      | `string`  | `"/data/diagnostics_effectiveness.json"` | Effectiveness-report JSON persistence path.                  |
| `diagnostics.recurrence_threshold`    | `integer` | `3`                                      | Occurrences within the window to trigger a recurrence alert. |
| `diagnostics.recurrence_window_days`  | `integer` | `30`                                     | Look-back window in days for recurrence detection.           |
| `diagnostics.observation_window_days` | `integer` | `30`                                     | Days after a fix to wait before an effectiveness report.     |

### Reference Docs (refdocs)

Read-only reference-docs tool — fetches documentation from allowlisted GitHub repos on demand.

| JSON key               | Type              | Default                    | Description                                |
| ---------------------- | ----------------- | -------------------------- | ------------------------------------------ |
| `refdocs.enabled`      | `boolean`         | `false`                    | Master switch. Requires non-empty `repos`. |
| `refdocs.repos`        | `array[string]`   | `[]`                       | Allowlist of `owner/name` GitHub repos.    |
| `refdocs.ref`          | `string`          | `"main"`                   | Default git ref/branch to read from.       |
| `refdocs.github_token` | `string` (secret) | `""`                       | Optional PAT for private repos.            |
| `refdocs.base_url`     | `string`          | `"https://api.github.com"` | Base URL for GitHub Enterprise.            |
| `refdocs.timeout`      | `number`          | `30.0`                     | Per-request HTTP timeout (seconds).        |

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

| JSON key                     | Type              | Default                    | Description                                 |
| ---------------------------- | ----------------- | -------------------------- | ------------------------------------------- |
| `version_check.enabled`      | `boolean`         | `false`                    | Master switch.                              |
| `version_check.repo`         | `string`          | `""`                       | GitHub `owner/name`. Required when enabled. |
| `version_check.github_token` | `string` (secret) | `""`                       | Optional PAT to avoid rate limits.          |
| `version_check.base_url`     | `string`          | `"https://api.github.com"` | Base URL for GitHub Enterprise.             |
| `version_check.timeout`      | `number`          | `30.0`                     | Per-request HTTP timeout (seconds).         |
| `version_check.cache_ttl`    | `number`          | `300.0`                    | Seconds to cache the latest-release lookup. |

### Component Client

HTTP client for inspecting and configuring remote component agents. Disabled by default.

| JSON key                      | Type            | Default | Description                                                                             |
| ----------------------------- | --------------- | ------- | --------------------------------------------------------------------------------------- |
| `component_client.enabled`    | `boolean`       | `false` | Master switch.                                                                          |
| `component_client.timeout`    | `number`        | `240.0` | Per-request HTTP timeout (seconds).                                                     |
| `component_client.components` | `array[object]` | `[]`    | List of component targets, each with `base_url` (string) and optional `label` (string). |

### Subsessions

Background sub-agent spawning configuration.

| JSON key                                                | Type            | Default                    | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------------- | --------------- | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `subsessions.max_concurrent`                            | `integer`       | `8`                        | Maximum concurrent subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `subsessions.max_depth`                                 | `integer`       | `3`                        | Maximum nesting depth.                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `subsessions.default_model_level`                       | `integer`       | `2`                        | Default model level for spawned subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `subsessions.min_interval_seconds`                      | `number`        | `60.0`                     | Minimum interval between periodic runs.                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `subsessions.auto_stop_no_change_runs`                  | `integer`       | `3`                        | Consecutive NO_CHANGE runs before auto-stop.                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `subsessions.max_idle_runs`                             | `integer`       | `3`                        | Consecutive NO_CHANGE runs before a periodic monitor auto-pauses (enters the real `paused` status). The monitor's worker stays alive and resumes automatically when the ticket state changes. Set to `0` to disable auto-pausing.                                                                                                                                                                                                                                            |
| `subsessions.run_timeout_seconds`                       | `number`        | `600.0`                    | Hard per-run timeout (seconds) for a single subsession turn. On expiry the run is marked failed and the schedule continues.                                                                                                                                                                                                                                                                                                                                                  |
| `subsessions.store_path`                                | `string`        | `"/data/subsessions.json`" | Path to the subsession persistence file.                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `subsessions.transcript_max_entries`                    | `integer`       | `200`                      | Maximum transcript entries per subsession.                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `subsessions.human_approval_timeout_runs`               | `integer`       | `5`                        | When a periodic subsession's checkpoint indicates the monitored ticket is in `human_issue_approval` state, auto-escalate (close with reason `human_approval_timeout`) after this many consecutive `NO_CHANGE` runs.                                                                                                                                                                                                                                                          |
| `subsessions.human_approval_timeout_seconds`            | `number`        | `300.0`                    | Wall-clock backstop for the `human_issue_approval` stuck-ticket gate. When the checkpoint has carried `last_known_state='human_issue_approval'` for longer than this many seconds, auto-escalate even if the `NO_CHANGE` run count has not yet reached `human_approval_timeout_runs`. Default 300 (5 minutes).                                                                                                                                                               |
| `subsessions.pre_authorized_ticket_patterns`            | `array[string]` | `[]`                       | Glob patterns (`fnmatch`) matching ticket IDs that are pre-authorized under a standing operator directive. When a monitored ticket's ID matches a pattern, the `human_issue_approval` gate is bypassed — the system auto-escalates immediately (reason `pre_authorized_approval`) instead of waiting for `human_approval_timeout_runs`.                                                                                                                                      |
| `subsessions.mill_recovery_initial_backoff_seconds`     | `number`        | `60.0`                     | Initial backoff (seconds) when a ticket monitor enters mill-recovery mode after consecutive failures. Doubles on each retry up to `mill_recovery_max_backoff_seconds`.                                                                                                                                                                                                                                                                                                       |
| `subsessions.mill_recovery_max_backoff_seconds`         | `number`        | `3600.0`                   | Maximum backoff (seconds) for mill-recovery retries (1 hour).                                                                                                                                                                                                                                                                                                                                                                                                                |
| `subsessions.mill_recovery_max_retries`                 | `integer`       | `10`                       | Maximum number of recovery retries before the subsession is permanently closed.                                                                                                                                                                                                                                                                                                                                                                                              |
| `subsessions.paused_monitor_poll_interval_seconds`      | `number`        | `60.0`                     | Interval (seconds) between polls of paused periodic monitors by the background watcher. The watcher checks each paused monitor's ticket state via the mill API; when the ticket's state differs from the checkpoint's `last_known_state` it sends an inbox wake message to the live paused worker (falling back to reopen+respawn if the worker is unreachable). Set to `0` to disable runtime polling (paused monitors only resume on service restart).                     |
| `subsessions.paused_monitor_long_poll_interval_seconds` | `number`        | `15.0`                     | Interval (seconds) between direct mill API polls by a paused periodic monitor in its wait loop. Each paused monitor polls the mill for its tracked ticket's state at this interval; when the state differs from the checkpoint's `last_known_state` the monitor resumes immediately (zero added latency). The background watcher's `paused_monitor_poll_interval_seconds` serves as a safety-net backup. Set to `0` to disable per-monitor long-polling (watcher-only wake). |
| `subsessions.periodic_max_interval_seconds`             | `number`        | `3600.0`                   | Upper bound (seconds) for a periodic subsession's self-adjusted interval. The `adjust_periodic_interval` tool clamps to this value. Default 3600 (1 hour).                                                                                                                                                                                                                                                                                                                   |
| `subsessions.periodic_max_total_runs`                   | `integer`       | `100`                      | Upper bound for a periodic subsession's self-adjusted `max_runs` (total run budget). The `adjust_periodic_budget` tool clamps to this value. Default 100.                                                                                                                                                                                                                                                                                                                    |
| `subsessions.transient_error_max_retries`               | `integer`       | `3`                        | Maximum retry attempts when a periodic subsession's agent turn fails with a transient API error (e.g. OpenRouter upstream hiccup). Retries use exponential backoff between `transient_error_backoff_base` and `transient_error_backoff_cap`. When retries are exhausted the cycle is skipped and the schedule continues rather than permanently failing the subsession.                                                                                                      |
| `subsessions.transient_error_backoff_base`              | `number`        | `1.0`                      | Initial backoff in seconds for transient-error retries (doubles each attempt).                                                                                                                                                                                                                                                                                                                                                                                               |
| `subsessions.transient_error_backoff_cap`               | `number`        | `30.0`                     | Maximum backoff in seconds for transient-error retries.                                                                                                                                                                                                                                                                                                                                                                                                                      |

### Feedback

Automated feedback analysis for continuous self-improvement. When enabled, a feedback run analyses
the conversation at compaction and session-end boundaries, then files improvement tickets via the
board's `POST /tickets/ingest` endpoint. Tickets flow through the normal human-approval workflow —
the feedback run never auto-approves. Disabled by default.

| JSON key                   | Type              | Default | Description                                                                |
| -------------------------- | ----------------- | ------- | -------------------------------------------------------------------------- |
| `feedback.enabled`         | `boolean`         | `false` | Master switch.                                                             |
| `feedback.model_level`     | `integer`         | `1`     | llmio capability level for the feedback-analysis agent (cheap extraction). |
| `feedback.board_url`       | `string`          | `""`    | Base URL of the board HTTP API (no trailing slash). Required when enabled. |
| `feedback.board_api_token` | `string` (secret) | `""`    | Optional Bearer token for the board API.                                   |
| `feedback.deploy_api_key`  | `string` (secret) | `""`    | Bearer / X-API-Key token for the central-deploy roster endpoint.           |
| `feedback.timeout`         | `number`          | `60.0`  | Per-request HTTP timeout (seconds) for ingest calls.                       |

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

| JSON key                                 | Type              | Default                    | Description                                                                                                                       |
| ---------------------------------------- | ----------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `direct_repo.enabled`                    | `boolean`         | `false`                    | Master switch.                                                                                                                    |
| `direct_repo.github_app_id`              | `string`          | `""`                       | GitHub App numeric or slug id. Required when enabled.                                                                             |
| `direct_repo.github_app_private_key`     | `string` (secret) | `""`                       | RSA private key in PEM format.                                                                                                    |
| `direct_repo.github_app_installation_id` | `string`          | `""`                       | Installation id to act as.                                                                                                        |
| `direct_repo.github_api_base_url`        | `string`          | `"https://api.github.com"` | Base URL for GitHub Enterprise.                                                                                                   |
| `direct_repo.board_api_base_url`         | `string`          | `"http://127.0.0.1:8077"`  | Board HTTP API base URL for ticket-state lookups.                                                                                 |
| `direct_repo.board_api_token`            | `string` (secret) | `""`                       | Optional bearer token for the board API.                                                                                          |
| `direct_repo.timeout`                    | `number`          | `30.0`                     | Per-request HTTP timeout (seconds).                                                                                               |
| `direct_repo.direct_fix_enabled`         | `boolean`         | `false`                    | Enables the `direct_fix` branch-push tool (requires `enabled`).                                                                   |
| `direct_repo.allow_push_to_existing_pr`  | `boolean`         | `false`                    | Expose `push_to_pr_branch` tool for committing small patches to existing PR branches (CI-fix iterations) without re-creating PRs. |

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

| JSON key                              | Type              | Default  | Description                                                                                                                                               |
| ------------------------------------- | ----------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lifecycle.enabled`                   | `boolean`         | `false`  | Master switch.                                                                                                                                            |
| `lifecycle.base_url`                  | `string`          | `""`     | Base URL of the deploy-lifecycle API (no trailing slash). If the URL has no scheme (e.g. `central-deploy:8100`), `default_protocol` is prepended.         |
| `lifecycle.default_protocol`          | `string`          | `"http"` | Protocol scheme prepended when `base_url` lacks one (e.g. `"https"` for TLS). Ignored when `base_url` already has a recognised scheme (`http`/`https`).   |
| `lifecycle.api_key`                   | `string` (secret) | `""`     | Optional API key for the deploy-lifecycle API.                                                                                                            |
| `lifecycle.service_name`              | `string`          | `""`     | Name of this service as registered with the deploy server.                                                                                                |
| `lifecycle.timeout`                   | `number`          | `30.0`   | Per-request HTTP timeout (seconds).                                                                                                                       |
| `lifecycle.self_restart_max_retries`  | `integer`         | `3`      | Maximum number of retries for transient `self_restart` failures (5xx, timeouts, connection errors). 0 = no retries.                                       |
| `lifecycle.self_restart_backoff_base` | `number`          | `1.0`    | Initial exponential-backoff delay in seconds. Doubled each retry: `base * 2^(attempt-1)`.                                                                 |
| `lifecycle.self_restart_backoff_cap`  | `number`          | `30.0`   | Maximum exponential-backoff delay in seconds (ceiling). Retries never wait longer than this.                                                              |
| `lifecycle.config_import_enabled`     | `boolean`         | `false`  | When `true`, attempt a one-time config import from central-deploy on first boot if no config file exists. Also gates the `POST /config/import` endpoint.  |
| `lifecycle.config_import_url`         | `string`          | `""`     | Optional override for the config-export endpoint URL. When empty, constructed from `base_url` as `{base_url}/chat/services/{service_name}/config/export`. |

______________________________________________________________________

### Notification

Browser notification settings — lets the agent alert the user proactively via the `notify_user`
tool. Enabled by default.

| JSON key               | Type      | Default | Description                                                    |
| ---------------------- | --------- | ------- | -------------------------------------------------------------- |
| `notification.enabled` | `boolean` | `true`  | Master switch. When `false`, no `notify_user` tool is offered. |

### HTTP Probe

Read-only HTTP uptime/render-probe tool for the agent. Enabled by default.

| JSON key                    | Type                        | Default                                | Description                                                                                                                                                                                                                                         |
| --------------------------- | --------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `http_probe.enabled`        | `boolean`                   | `true`                                 | Master switch. When `false`, no `http_probe` tool is offered.                                                                                                                                                                                       |
| `http_probe.timeout`        | `number`                    | `10.0`                                 | Per-request HTTP timeout (seconds).                                                                                                                                                                                                                 |
| `http_probe.allowlist`      | `array[string]`             | `["www.robotsix.net", "robotsix.net"]` | Hostnames the tool is permitted to probe. Empty permits any public hostname.                                                                                                                                                                        |
| `http_probe.max_body_bytes` | `integer`                   | `2048`                                 | Maximum bytes of the response body to return (~2 KB).                                                                                                                                                                                               |
| `http_probe.max_redirects`  | `integer`                   | `5`                                    | Maximum number of redirects to follow.                                                                                                                                                                                                              |
| `http_probe.fleet_auth`     | `FleetAuthSettings \| null` | `null`                                 | Optional server-side credentials for authenticated fleet UIs. When set, requests to hosts in `fleet_auth.auth_hosts` carry HTTP basic-auth headers injected by the server (never visible to the agent), and those hosts are implicitly allowlisted. |

The `FleetAuthSettings` object accepts:

| JSON key                         | Type              | Default | Description                                                                                                                                   |
| -------------------------------- | ----------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `fleet_auth.basic_auth_username` | `string`          | `""`    | Username for HTTP basic authentication. Leave empty when auth is not required.                                                                |
| `fleet_auth.basic_auth_password` | `string` (secret) | `""`    | Password for HTTP basic authentication (`SecretStr` — never serialised in logs or exposed to the agent).                                      |
| `fleet_auth.auth_hosts`          | `array[string]`   | `[]`    | Hostnames (no protocol, no path) for which the basic-auth header is attached. Requests to hosts not on this list proceed without credentials. |

### Autonomous

Autonomous sessions that pick a subject, draft a plan for operator review, then execute after the
operator comments. Sessions stay open after completion — the operator must explicitly close them.

Multiple *named session definitions* can be configured in `autonomous.sessions`, each with its own
prompt and trigger. When the list is empty, a single default preset matching the pre-existing
behavior is synthesized at runtime — backward compatible out of the box.

| JSON key                                          | Type      | Default                            | Description                                                                                                                                                                                                                         |
| ------------------------------------------------- | --------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `autonomous.enabled`                              | `boolean` | `true`                             | Master switch.                                                                                                                                                                                                                      |
| `autonomous.proposal_marker`                      | `string`  | `"---PROPOSAL READY---"`           | Marker string the agent emits after drafting a plan to signal it is ready for operator review. The session enters the `proposal` state.                                                                                             |
| `autonomous.completion_marker`                    | `string`  | `"---AUTONOMOUS COMPLETE---"`      | Marker string the agent emits when the plan is complete. The session stays open after completion.                                                                                                                                   |
| `autonomous.max_auto_turns`                       | `integer` | `20`                               | Maximum automatic agent turns during the execution phase before reverting to `proposal`.                                                                                                                                            |
| `autonomous.max_idle_auto_turns`                  | `integer` | `5`                                | Maximum number of consecutive NO_CHANGE / idle auto-continue turns before the loop halts (reverts to `proposal`). Set to `0` to disable the idle cap and only rely on `max_auto_turns`.                                             |
| `autonomous.persist_path`                         | `string`  | `"/data/autonomous_sessions.json"` | Path to the autonomous-session persistence file.                                                                                                                                                                                    |
| `autonomous.session_color`                        | `string`  | `""`                               | Optional CSS color string for a visual accent on autonomous session rows (e.g. `"#ef4444"`).                                                                                                                                        |
| `autonomous.initial_task`                         | `string`  | `""`                               | Optional description of the first task to spawn. When empty, the agent picks its own subject.                                                                                                                                       |
| `autonomous.continue_interval_seconds`            | `number`  | `45.0`                             | Pacing interval (seconds) between auto-continue loop iterations.                                                                                                                                                                    |
| `autonomous.pending_subsession_wait_timeout`      | `number`  | `600.0`                            | Maximum time (seconds) the auto-continue loop waits for pending non-periodic subsessions to complete before giving up and continuing.                                                                                               |
| `autonomous.stale_monitor_runs_before_completion` | `integer` | `3`                                | Number of consecutive `NO_CHANGE` cycles after which a periodic monitor is considered "stale" — the agent may declare the autonomous session complete even while the monitor is still running. Monitors continue in the background. |
| `autonomous.sessions`                             | `array`   | `[]`                               | List of named autonomous session definitions (see below). When empty, a single default preset is synthesized.                                                                                                                       |

Each entry in `autonomous.sessions` is an `AutonomousSessionDefinition` object:

| JSON key                   | Type      | Default      | Description                                                                                         |
| -------------------------- | --------- | ------------ | --------------------------------------------------------------------------------------------------- |
| `name`                     | `string`  | *(required)* | Unique identifier for this session definition.                                                      |
| `prompt`                   | `string`  | `""`         | Custom kickoff prompt. When empty, the standard "Pick a subject and draft a plan" prompt is used.   |
| `trigger_type`             | `string`  | `"periodic"` | Restart strategy: `"periodic"` (wait `trigger_interval_seconds`) or `"on_close"` (continuous mode). |
| `trigger_interval_seconds` | `number`  | `45.0`       | Delay between completion and restart for `"periodic"` trigger. Ignored for `"on_close"`.            |
| `enabled`                  | `boolean` | `true`       | When `false`, the definition is skipped — no session is created for it.                             |

**Default preset.** When `autonomous.sessions` is empty (the default), the runner synthesizes a
single session definition named `"default"` with a periodic trigger at `continue_interval_seconds`.
This preserves the pre-existing single-session behavior exactly — the
`GET /sessions?owner_id=autonomous` endpoint and the `[AUTONOMOUS]` UI badge continue to work.

**Named sessions.** Adding entries to `autonomous.sessions` enables multiple concurrent autonomous
sessions. Each definition maps to a distinct pseudo-owner (`autonomous:<name>`), so sessions cannot
overlap with themselves (the per-owner dedup invariant applies). Session runs are logged and
auditable — each run records the definition name, trigger reason, start/end time, and summary.

**API.** The management surface is served at:

- `GET /autonomous/definitions` — list all definitions with their current active session.
- `POST /autonomous/definitions/{name}/run` — manually trigger a one-shot run (returns 409 if a
  session is already active).

**Example** — two autonomous sessions, one periodic and one continuous:

```json
"autonomous": {
  "enabled": true,
  "sessions": [
    {
      "name": "default",
      "prompt": "",
      "trigger_type": "periodic",
      "trigger_interval_seconds": 45.0,
      "enabled": true
    },
    {
      "name": "continuous-triage",
      "prompt": "Begin an autonomous triage session.  Scan open tickets and investigate the oldest unassigned item.",
      "trigger_type": "on_close",
      "enabled": true
    }
  ]
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

| JSON key                     | Type                        | Default | Description                                                                                                                                                                                             |
| ---------------------------- | --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `render_url.enabled`         | `boolean`                   | `false` | Master switch.                                                                                                                                                                                          |
| `render_url.timeout`         | `number`                    | `30.0`  | Per-request page-load timeout (seconds).                                                                                                                                                                |
| `render_url.viewport_width`  | `integer`                   | `1280`  | Browser viewport width (pixels).                                                                                                                                                                        |
| `render_url.viewport_height` | `integer`                   | `720`   | Browser viewport height (pixels).                                                                                                                                                                       |
| `render_url.fleet_auth`      | `FleetAuthSettings \| null` | `null`  | Optional server-side credentials for authenticated fleet UIs. When set, requests to hosts in `fleet_auth.auth_hosts` carry HTTP basic-auth headers injected by the server (never visible to the agent). |

The `FleetAuthSettings` object accepts:

| JSON key                         | Type              | Default | Description                                                                                                                                   |
| -------------------------------- | ----------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `fleet_auth.basic_auth_username` | `string`          | `""`    | Username for HTTP basic authentication. Leave empty when auth is not required.                                                                |
| `fleet_auth.basic_auth_password` | `string` (secret) | `""`    | Password for HTTP basic authentication (`SecretStr` — never serialised in logs or exposed to the agent).                                      |
| `fleet_auth.auth_hosts`          | `array[string]`   | `[]`    | Hostnames (no protocol, no path) for which the basic-auth header is attached. Requests to hosts not on this list proceed without credentials. |

______________________________________________________________________

### Public Fetch

Scoped public-repo-fetch tool for the chat agent. When enabled, the agent gains a `fetch_public_url`
tool that performs a plain HTTP(S) GET to a user-provided public URL, returns the raw text/file
contents with metadata, and writes an audit-log entry per fetch. SSRF protection blocks
internal/private IP ranges for public hosts; hosts listed in `fleet_auth.auth_hosts` are trusted by
the operator and bypass the SSRF check.

| JSON key                                 | Type                        | Default   | Description                                                                                                                                                                                                                                                                                  |
| ---------------------------------------- | --------------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `public_fetch.enabled`                   | `boolean`                   | `false`   | Master switch. When `False`, no tool is offered.                                                                                                                                                                                                                                             |
| `public_fetch.timeout`                   | `number`                    | `10.0`    | Per-request HTTP timeout in seconds.                                                                                                                                                                                                                                                         |
| `public_fetch.max_body_bytes`            | `integer`                   | `1048576` | Maximum bytes of the response body to read and return to the agent (~1 MB).                                                                                                                                                                                                                  |
| `public_fetch.max_redirects`             | `integer`                   | `5`       | Maximum number of redirects to follow.                                                                                                                                                                                                                                                       |
| `public_fetch.domain_allowlist`          | `array[string]`             | `[]`      | Optional list of hostnames (no protocol, no path) permitted for fetch. Empty = any public host is allowed.                                                                                                                                                                                   |
| `public_fetch.rate_limit_requests`       | `integer`                   | `10`      | Maximum requests allowed within `rate_limit_window_seconds`.                                                                                                                                                                                                                                 |
| `public_fetch.rate_limit_window_seconds` | `number`                    | `60.0`    | Sliding window in seconds for the rate limiter.                                                                                                                                                                                                                                              |
| `public_fetch.fleet_auth`                | `FleetAuthSettings \| null` | `null`    | Optional server-side credentials for authenticated fleet UIs. When set, requests to hosts in `fleet_auth.auth_hosts` carry HTTP basic-auth headers injected by the server (never visible to the agent), and those hosts are implicitly allowed through the domain allowlist and SSRF checks. |

The `FleetAuthSettings` object accepts:

| JSON key                         | Type              | Default | Description                                                                                                                                   |
| -------------------------------- | ----------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `fleet_auth.basic_auth_username` | `string`          | `""`    | Username for HTTP basic authentication. Leave empty when auth is not required.                                                                |
| `fleet_auth.basic_auth_password` | `string` (secret) | `""`    | Password for HTTP basic authentication (`SecretStr` — never serialised in logs or exposed to the agent).                                      |
| `fleet_auth.auth_hosts`          | `array[string]`   | `[]`    | Hostnames (no protocol, no path) for which the basic-auth header is attached. Requests to hosts not on this list proceed without credentials. |

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

### Docker Digest

Docker image digest resolution tool for the agent. When enabled, the agent gains a
`resolve_docker_digest` tool that resolves a Docker image reference (e.g. `python:3.14-slim`) and
target platform to its immutable `sha256:...` content digest by querying the Docker Registry v2 HTTP
API. Used by tooling that needs pinned image digests (e.g. CI workflows, Dockerfiles).

| JSON key                      | Type      | Default                          | Description                                                                  |
| ----------------------------- | --------- | -------------------------------- | ---------------------------------------------------------------------------- |
| `docker_digest.enabled`       | `boolean` | `true`                           | Master switch. When `false`, no docker_digest tool is offered to the agent.  |
| `docker_digest.timeout`       | `number`  | `30.0`                           | Per-request HTTP timeout in seconds for registry API calls.                  |
| `docker_digest.registry_host` | `string`  | `"registry-1.docker.io"`         | Docker Registry v2 hostname for manifest lookups.                            |
| `docker_digest.auth_url`      | `string`  | `"https://auth.docker.io/token"` | Token-authentication endpoint for bearer tokens (Docker Hub's auth service). |

______________________________________________________________________

## Schema

The committed `config/config.schema.json` is the authoritative schema for the `Settings` model. It
is auto-generated from the pydantic model via `Settings.model_json_schema()` and **CI-checked** to
stay in sync — a CI job regenerates it from the model and fails the build on any drift.

To regenerate locally:

```bash
python -c 'import json; from robotsix_chat.config import Settings; print(json.dumps(Settings.model_json_schema(), indent=2))' > config/config.schema.json
```
