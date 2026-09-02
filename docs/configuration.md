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
- `openrouter.keys.<alias>`, `memory.embedding.api_key`
- `central_deploy.deploy_api_key`
- `direct_repo.github_app_private_key`, `direct_repo.board_api_token`
- `feedback.board_api_token`

## Settings reference

All fields and their defaults are listed in `config/config.json`. The sections below describe each
group.

______________________________________________________________________

### Top-level

| JSON key                        | Type                | Default                                               | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------- | ------------------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `llmio_model_level`             | `integer`           | `2`                                                   | LLM capability level: `1` (cheap/frequent), `2` (workhorse, default), `3` (frontier). Levels are a pure capability axis; the serving provider is llmio's failover axis (keyless Claude SDK default slot, keyed OpenRouter fallback slot).                                                                                                                                                                                                                                                                                                                                                                 |
| `llmio_api_key`                 | `string` (secret)   | `""`                                                  | OpenRouter API key. Required for levels 1–2; ignored for 3–4.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `chat_model_level`              | `integer` or `null` | `null`                                                | Optional override of `llmio_model_level` for the main interactive chat agent. When `null` (default), the chat agent uses `llmio_model_level`. Set to a specific level (e.g. `3` for the frontier tier) to route chat turns to a different level while other consumers (subsessions, periodic, summary) still use `llmio_model_level` or their own overrides.                                                                                                                                                                                                                                              |
| `summary_model_level`           | `integer`           | `1`                                                   | Capability level of the dedicated summariser agent (idle-timeout compaction summary, carryover summary, conversation titles). Runs once per idle gap, not per turn; a bounded text transformation, so the cheap/frequent level.                                                                                                                                                                                                                                                                                                                                                                           |
| `llmio_failover_window_seconds` | `number`            | `900`                                                 | How long llmio routes calls straight to the fallback (OpenRouter) provider slot after the default (Claude) slot fails repeatedly or exhausts its quota, before automatically returning to the default.                                                                                                                                                                                                                                                                                                                                                                                                    |
| `agent_instruction`             | `string`            | (long default)                                        | System instruction for the agent. Governed by the code default in `src/robotsix_chat/config/settings.py` (currently v160). Intentionally absent from `config/config.json` — the code default is the single source of truth. Operators who need to override it can add `"agent_instruction"` to their local or deployed config file; doing so bypasses the code default entirely. The agent's reply style is governed separately by [`docs/prompt-style.md`](prompt-style.md) — that file is automatically injected into every system prompt build and is the single source of truth for reply formatting. |
| `max_images_per_message`        | `integer`           | `8`                                                   | Maximum images per chat message.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `max_image_bytes`               | `integer`           | `5242880`                                             | Maximum image size in bytes (5 MiB).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `allowed_image_media_types`     | `array[string]`     | `["image/png","image/jpeg","image/gif","image/webp"]` | Allowed image MIME types.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

### Server

| JSON key                       | Type            | Default          | Description                                                                                        |
| ------------------------------ | --------------- | ---------------- | -------------------------------------------------------------------------------------------------- |
| `server_host`                  | `string`        | `"0.0.0.0"`      | Host the server binds to.                                                                          |
| `server_port`                  | `integer`       | `8000`           | Port the server listens on.                                                                        |
| `idle_timeout_minutes`         | `integer`       | `30`             | Minutes of inactivity before closing the connection.                                               |
| `compaction_min_turns`         | `integer`       | `3`              | DEPRECATED — unused. Idle compaction was removed; see `evergoing.min_fresh_turns`.                 |
| `compaction_keep_recent_turns` | `integer`       | `2`              | DEPRECATED — unused. Idle compaction was removed; see `evergoing.keep_min_recent`.                 |
| `log_level`                    | `string`        | `"INFO"`         | Python logging level.                                                                              |
| `log_json_format`              | `boolean`       | `true`           | When `true`, log lines are structured JSON (structlog); `false` for human-readable console output. |
| `cors_allow_origins`           | `array[string]` | `[]`             | Origins allowed to call `/chat` cross-origin.                                                      |
| `correlation_id_header`        | `string`        | `"X-Request-ID"` | Header name for request correlation ids.                                                           |

**Context reduction — one mechanism.** Idle-timeout compaction was removed. The subject-aware trim
scheduler (see the Evergoing section) is the single way ANY session's context shrinks: every
`evergoing.trim_interval_seconds` it inspects each session with new input, and only when a cheap
decision model judges the subject clearly changed does it drop the finished leading turns.
`evergoing.min_fresh_turns` gates the decision model so tiny or freshly-trimmed sessions are never
summarised or churned.

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

### OpenRouter

The canonical component-standard OpenRouter credential block: provider API keys keyed by the
**alias** each LLM-generating subsystem is billed under. The alias matches the subsystem's Langfuse
project name, so cost-monitor can join OpenRouter provider spend to Langfuse traces via the shared
alias.

| JSON key                  | Type              | Default | Description                                                          |
| ------------------------- | ----------------- | ------- | -------------------------------------------------------------------- |
| `openrouter.keys.<alias>` | `string` (secret) | —       | OpenRouter API key for the LLM-generating subsystem named `<alias>`. |

The main chat agent runs on the Claude SDK and needs no OpenRouter key, so this component declares
only one alias:

- `robotsix-chat-cognee` — the cognee memory extraction LLM, matching `memory.langfuse_project` (and
  the `langfuse.projects` entry of the same name).

```json
"openrouter": {
  "keys": {
    "robotsix-chat-cognee": "sk-or-..."  // pragma: allowlist secret
  }
}
```

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

| JSON key                                       | Type              | Default                          | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------------------------------------- | ----------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.enabled`                               | `boolean`         | `false`                          | Master switch. Requires cognee extras.                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `memory.background_recall_enabled`             | `boolean`         | `true`                           | When `true` (default), background agents (subsessions and periodic sessions) may READ memory even when their write gate is off — they get recall plus the `search_memory` tool, but `remember` is a no-op. Recall is a retrieval-only lookup while cognify is a multi-minute LLM pipeline, so there is no reason to deny background agents the accumulated context just because they must not pay to write it back. Set `false` to restore the previous all-or-nothing behaviour. |
| `memory.subsession_enabled`                    | `boolean`         | `false`                          | When `true`, subsession agents (task / periodic / user_chat workers) get full memory (recall + cognify). Default `false` — background agents run continuously and would otherwise accrue cognee cost 24/7.                                                                                                                                                                                                                                                                        |
| `memory.periodic_enabled`                      | `boolean`         | `false`                          | When `true`, periodic session agents get full memory. Default `false` for the same cost reason as `subsession_enabled`. Independent toggle so the two background classes can be gated separately.                                                                                                                                                                                                                                                                                 |
| `memory.data_dir`                              | `string`          | `"/data/cognee"`                 | Cognee store directory (keep on persistent volume).                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `memory.recall_search_type`                    | `string`          | `"CHUNKS"`                       | Cognee `SearchType` for the automatic per-message recall. Pure retrieval, no LLM call, so every chat turn stays cheap and fast. Deep, LLM-mediated search is available on demand via `deep_recall_search_type` and the `search_memory` tool.                                                                                                                                                                                                                                      |
| `memory.recall_timeout_seconds`                | `number`          | `60.0`                           | Hard timeout (seconds) for a single automatic `recall` call. On expiry degrades to empty string — the agent proceeds without memory.                                                                                                                                                                                                                                                                                                                                              |
| `memory.recall_max_concurrency`                | `integer`         | `4`                              | Maximum number of parallel recall operations that may run inside cognee at once.                                                                                                                                                                                                                                                                                                                                                                                                  |
| `memory.deep_recall_search_type`               | `string`          | `"GRAPH_COMPLETION"`             | Cognee `SearchType` for the on-demand `search_memory` tool. The expensive, LLM-mediated graph search — paid only when the agent deliberately asks for it instead of on every turn.                                                                                                                                                                                                                                                                                                |
| `memory.deep_recall_timeout_seconds`           | `number`          | `180.0`                          | Hard timeout (seconds) for one `search_memory` tool call. More generous than the automatic recall's — the tool is invoked deliberately, so waiting longer is acceptable where stalling every message was not.                                                                                                                                                                                                                                                                     |
| `memory.remember_timeout_seconds`              | `number`          | `900.0`                          | Hard timeout (seconds) for ONE `remember` attempt (cognify consolidation). Default 900 s — raised from 300 after consecutive timeouts: cognify is a multi-minute LLM pipeline contending with recall for cognee's stores.                                                                                                                                                                                                                                                         |
| `memory.remember_max_attempts`                 | `integer`         | `3`                              | How many times a write is attempted before the exchange is parked in the backlog. Each attempt gets the full `remember_timeout_seconds`; failures back off exponentially via `robotsix_http.acall_with_retry`.                                                                                                                                                                                                                                                                    |
| `memory.write_backlog_path`                    | `string`          | `"/data/cognee/backlog.jsonl"`   | Path to a durable JSONL backlog for exchanges that could not be persisted after retries are exhausted. Drained opportunistically on subsequent successful writes.                                                                                                                                                                                                                                                                                                                 |
| `memory.datafusion_runtime_memory_limit`       | `string`          | `"256M"`                         | DataFusion memory-pool limit (e.g. `"256M"`, `"1G"`). Bounds the LanceDB worker subprocess memory so a single large `merge_insert` does not OOM the container. Safe default for 2 GB containers; raise for larger limits.                                                                                                                                                                                                                                                         |
| `memory.frozen_store_alert_minutes`            | `number`          | `10.0`                           | Duration (minutes) of consecutive write failures before a `WARNING` diagnostic is emitted — prevents a silently frozen vector store from going unnoticed for days.                                                                                                                                                                                                                                                                                                                |
| `memory.auto_recovery_enabled`                 | `boolean`         | `true`                           | When `true` (default), a freeze that persists past `frozen_store_recovery_minutes` triggers a guarded self-restart. Requires `lifecycle.enabled` and `lifecycle.service_name`.                                                                                                                                                                                                                                                                                                    |
| `memory.frozen_store_recovery_minutes`         | `number`          | `15.0`                           | Freeze duration (minutes) after which auto-recovery self-restart is attempted. Should exceed `frozen_store_alert_minutes` so the store is surfaced as degraded before restart.                                                                                                                                                                                                                                                                                                    |
| `memory.recovery_cooldown_minutes`             | `number`          | `30.0`                           | Minimum interval (minutes) between auto-recovery self-restart attempts — prevents restart loops when the store re-freezes immediately.                                                                                                                                                                                                                                                                                                                                            |
| `memory.write_throttle_seconds`                | `number`          | `0.5`                            | Delay (seconds) between serialised writes so the LanceDB worker subprocess can complete each `merge_insert` before the next starts. Prevents burst OOM.                                                                                                                                                                                                                                                                                                                           |
| `memory.maintenance_enabled`                   | `boolean`         | `true`                           | When `true` (default), a background task periodically compacts fragments and prunes old versions of every table in the cognee LanceDB store (LanceDB `Table.optimize`). Without it every `cognify` write appends a fragment/version/deletion file that is never merged, so vector search scans thousands of tiny fragments — starving recall and saturating the host disk. The pass runs under the write lock and processes tables sequentially.                                  |
| `memory.maintenance_interval_seconds`          | `number`          | `21600.0`                        | Seconds between LanceDB maintenance passes; the first pass runs at startup. Default `21600.0` (6 h).                                                                                                                                                                                                                                                                                                                                                                              |
| `memory.maintenance_version_retention_seconds` | `number`          | `3600.0`                         | Age (seconds) below which LanceDB dataset versions are kept during pruning (`cleanup_older_than`). Older versions are removed so the on-disk version count stays bounded. Default `3600.0` (1 h).                                                                                                                                                                                                                                                                                 |
| `memory.llm.provider`                          | `string`          | `"custom"`                       | Extraction LLM provider.                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `memory.llm.model`                             | `string`          | `"openrouter/openai/gpt-5-nano"` | Extraction LLM model.                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `memory.llm.endpoint`                          | `string`          | `"https://openrouter.ai/api/v1"` | Extraction LLM endpoint.                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `memory.llm.max_completion_tokens`             | `integer`         | `1024`                           | Maximum completion tokens per extraction LLM call. Caps output verbosity to control cost.                                                                                                                                                                                                                                                                                                                                                                                         |
| `memory.embedding.provider`                    | `string`          | `"openai_compatible"`            | Embedding provider.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `memory.embedding.model`                       | `string`          | `"bge-m3"`                       | Embedding model name.                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `memory.embedding.endpoint`                    | `string`          | `""`                             | Embedding server URL (e.g. `http://host:11434/v1`).                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `memory.embedding.dimensions`                  | `integer`         | `1024`                           | Embedding vector dimensions.                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `memory.embedding.api_key`                     | `string` (secret) | `""`                             | Bearer token for the embedding server.                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `memory.embedding.huggingface_tokenizer`       | `string`          | `"BAAI/bge-m3"`                  | HuggingFace tokenizer name.                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `memory.langfuse_project`                      | `string`          | `"robotsix-chat-cognee"`         | Name of the Langfuse project cognee's own LLM traffic traces to; its credentials are resolved from the top-level `langfuse` block.                                                                                                                                                                                                                                                                                                                                                |

### Central Deploy

Component-access roster and skill loading from the central-deploy management plane.

| JSON key                                                        | Type              | Default  | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------------------------------------- | ----------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `central_deploy.url`                                            | `string`          | `""`     | Canonical base URL of the central-deploy / deploy-lifecycle API (no trailing slash). Single source of truth for the deploy-plane address; the lifecycle client and feedback roster lookup both read it.                                                                                                                                                                                                                                                                      |
| `central_deploy.deploy_api_key`                                 | `string` (secret) | `""`     | Canonical deploy-plane credential — the shared secret between this chat component and central-deploy. Sent as the `X-API-Key` header on outbound roster/lifecycle calls, and required (matched) on inbound central-deploy → chat endpoints.                                                                                                                                                                                                                                  |
| `central_deploy.roster_cache_ttl`                               | `number`          | `300.0`  | Seconds to cache the component roster before re-fetching.                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `central_deploy.component_response_max_chars`                   | `integer`         | `200000` | Default truncation limit for GET/HEAD component responses — write methods keep the 8,000-char limit. Raised from 8,000 so large ticket lists (e.g. mill board blocked tickets) enumerate fully. Each call can override this with `component_request`'s `max_response_chars` parameter (e.g. `max_response_chars=2000` for a compact summary of a ticket history).                                                                                                            |
| `central_deploy.component_request_timeout`                      | `number`          | `60.0`   | Per-request HTTP timeout (seconds) for component API calls made via component_request. Also acts as the wall-clock deadline for all retry attempts so a failing component cannot block the agent indefinitely. Default 60s — raise this if upstream components are genuinely slow to respond.                                                                                                                                                                                |
| `central_deploy.component_fallbacks`                            | `object`          | `{}`     | Baked-in fallback base URLs for components that may be missing from the central-deploy roster (e.g. after a redeploy). Keyed by component id (e.g. `"robotsix-mill"` → `"http://mill:8080"`). When the roster returned by central-deploy is missing a component, the fallback URL is used instead — keeps monitors and tool calls running through transient roster gaps. If a component is reported as unknown, the error message tells you exactly which config key to set. |
| `central_deploy.component_credentials.<id>.basic_auth_username` | `string` (secret) | `""`     | Username for HTTP Basic auth to component `<id>`.                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `central_deploy.component_credentials.<id>.basic_auth_password` | `string` (secret) | `""`     | Password for HTTP Basic auth to component `<id>`.                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `central_deploy.component_credentials.<id>.header_token`        | `string` (secret) | `""`     | Token for header-based auth (e.g. `X-API-Key`) to component `<id>`.                                                                                                                                                                                                                                                                                                                                                                                                          |

Keys are component IDs matching the central-deploy roster (the `id` field in
`GET /chat/components`). The roster entry's `auth.type` selects which credential fields are used —
only the fields matching the declared auth scheme are consulted when authenticating.

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

### Continuation

Post-restart auto-resume capability. When enabled, the agent gains tools to schedule, cancel, and
query a continuation that fires automatically on the next boot — used to resume work-in-progress
after a self-restart without human intervention. Disabled by default.

| JSON key                       | Type      | Default                     | Description                                                                                        |
| ------------------------------ | --------- | --------------------------- | -------------------------------------------------------------------------------------------------- |
| `continuation.enabled`         | `boolean` | `false`                     | Master switch. When `false`, no continuation tools are offered.                                    |
| `continuation.store_path`      | `string`  | `"/data/continuation.json"` | Path to the JSON persistence file. Must be on a persistent volume to survive container recreation. |
| `continuation.max_consecutive` | `integer` | `3`                         | Maximum consecutive auto-continuations before the guardrail blocks further automatic firing.       |

### Evergoing

The single never-ending "evergoing" session. When enabled, exactly one evergoing session is created
on boot (idempotent, kept across restarts): it appears in the operator's session list flagged
`evergoing` and is never auto-closed or auto-evicted. A background scheduler runs every
`evergoing.trim_interval_seconds` and calls the new-input gate **first** — a no-input interval makes
zero LLM calls. When new turns have arrived, a cheap summary-tier model decides whether the
conversation's subject has clearly changed and how many finished leading turns to drop, then those
turns are physically trimmed from both the agent view and the UI transcript (distinct from the
summary/compaction card, which keeps the full transcript). The most-recent `keep_min_recent` turns
are never trimmed, so the in-flight turn is always preserved. Disabled by default — set
`evergoing.enabled` to `true` to activate.

| JSON key                          | Type      | Default  | Description                                                                                                                                                                  |
| --------------------------------- | --------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `evergoing.enabled`               | `boolean` | `false`  | Master switch for the evergoing session itself. The subject-aware trim scheduler always runs over all sessions.                                                              |
| `evergoing.trim_interval_seconds` | `number`  | `1800.0` | Seconds between scheduled subject-aware trim passes. Must be >0. Default 1800 (30 minutes).                                                                                  |
| `evergoing.keep_min_recent`       | `integer` | `2`      | Minimum most-recent turns the trim pass always keeps — guarantees the in-flight turn is never trimmed.                                                                       |
| `evergoing.min_fresh_turns`       | `integer` | `3`      | Minimum fresh turns since the last trim before the decision model is consulted; the skip does not advance the watermark, so short exchanges accumulate until the gate opens. |

### Subsessions

Background sub-agent spawning configuration.

| JSON key                                                | Type      | Default                    | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------------- | --------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `subsessions.max_concurrent`                            | `integer` | `8`                        | Maximum concurrent subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `subsessions.max_concurrent_per_session`                | `integer` | `0`                        | Per-session cap on active subsessions. When a session reaches this limit, new spawns are rejected even if the global pool has room. Set to `0` to disable (no per-session limit).                                                                                                                                                                                                                                                                                                                            |
| `subsessions.stale_reclaim_seconds`                     | `number`  | `0.0`                      | When the global pool is full but the spawning session is under its per-session limit, SLEEPING or PAUSED subsessions from other sessions that have been idle for longer than this many seconds are reclaimed (closed) to free a slot. SLEEPING subsessions are preferred over PAUSED because they count against the global capacity cap; reclaiming one actually frees a slot. Set to `0` to disable.                                                                                                        |
| `subsessions.max_depth`                                 | `integer` | `3`                        | Maximum nesting depth.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `subsessions.default_model_level`                       | `integer` | `2`                        | Default model level for spawned subsessions (the workhorse level). Provider availability is llmio's failover concern — long-lived background trackers survive provider outages via the fallback slot.                                                                                                                                                                                                                                                                                                        |
| `subsessions.delegated_read_model_level`                | `integer` | `1`                        | Default model level for children spawned (without an explicit `model_level`) by a frontier parent subsession (level `3`). Frontier agents are instructed to fan bulk reading/extraction out to cheap children; this makes that fan-out land on the cheap level by default while the parent can still override `model_level` when a subtask genuinely needs reasoning.                                                                                                                                        |
| `subsessions.monitor_max_model_level`                   | `integer` | `1`                        | Maximum model level for periodic and wait_for_event monitor subsessions. Routine monitors (ticket polling, periodic checks) are capped at the cheap/frequent level so they never burn workhorse or frontier capacity. Set to `3` to remove the cap.                                                                                                                                                                                                                                                          |
| `subsessions.min_interval_seconds`                      | `number`  | `60.0`                     | Minimum interval between periodic runs.                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `subsessions.auto_stop_no_change_runs`                  | `integer` | `3`                        | Consecutive NO_CHANGE runs before auto-stop.                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `subsessions.max_idle_runs`                             | `integer` | `15`                       | Consecutive NO_CHANGE runs before a periodic monitor auto-pauses (enters the real `paused` status). The monitor's worker stays alive and resumes automatically when the ticket state changes. Set to `0` to disable auto-pausing.                                                                                                                                                                                                                                                                            |
| `subsessions.max_no_change_pauses`                      | `integer` | `3`                        | Consecutive no-change pauses before a periodic monitor auto-closes with reason `no_change_pause_limit` instead of pausing again. Resets when the monitor observes a real change. Set to `0` to disable (always pause).                                                                                                                                                                                                                                                                                       |
| `subsessions.run_timeout_seconds`                       | `number`  | `600.0`                    | Hard per-run timeout (seconds) for a single subsession turn. On expiry the run is marked failed and the schedule continues.                                                                                                                                                                                                                                                                                                                                                                                  |
| `subsessions.store_path`                                | `string`  | `"/data/subsessions.json`" | Path to the subsession persistence file.                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `subsessions.transcript_max_entries`                    | `integer` | `200`                      | Maximum transcript entries per subsession.                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `subsessions.human_approval_timeout_runs`               | `integer` | `5`                        | When a periodic subsession's checkpoint indicates the monitored ticket is in `human_issue_approval` state, auto-escalate (close with reason `human_approval_timeout`) after this many consecutive `NO_CHANGE` runs.                                                                                                                                                                                                                                                                                          |
| `subsessions.human_approval_timeout_seconds`            | `number`  | `300.0`                    | Wall-clock backstop for the `human_issue_approval` stuck-ticket gate. When the checkpoint has carried `last_known_state='human_issue_approval'` for longer than this many seconds, auto-escalate even if the `NO_CHANGE` run count has not yet reached `human_approval_timeout_runs`. Default 300 (5 minutes).                                                                                                                                                                                               |
| `subsessions.auto_drive_promote_ready_drafts`           | `boolean` | `false`                    | Opt-in gate for the auto-drive monitor's promotable-draft branch. When `true` and a monitored ticket is a promotable draft (state `draft`, refine-complete spec, no open blocking review thread), the monitor transitions it into the ready queue. When `false` (default) the monitor never auto-promotes — it posts at most one operator-decision comment and then waits event-driven for the operator's promote/close decision, without consuming further run budget.                                      |
| `subsessions.mill_recovery_initial_backoff_seconds`     | `number`  | `60.0`                     | Initial backoff (seconds) when a ticket monitor enters mill-recovery mode after consecutive failures. Doubles on each retry up to `mill_recovery_max_backoff_seconds`.                                                                                                                                                                                                                                                                                                                                       |
| `subsessions.mill_recovery_max_backoff_seconds`         | `number`  | `3600.0`                   | Maximum backoff (seconds) for mill-recovery retries (1 hour).                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `subsessions.mill_recovery_max_retries`                 | `integer` | `10`                       | Maximum number of recovery retries before the subsession is permanently closed.                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `subsessions.paused_monitor_poll_interval_seconds`      | `number`  | `60.0`                     | Interval (seconds) between polls of paused periodic monitors by the background watcher. The watcher checks each paused monitor's ticket state via the mill API; when the ticket's state differs from the checkpoint's `last_known_state` it sends an inbox wake message to the live paused worker (falling back to reopen+respawn if the worker is unreachable). Set to `0` to disable runtime polling (paused monitors only resume on service restart).                                                     |
| `subsessions.paused_monitor_long_poll_interval_seconds` | `number`  | `15.0`                     | Interval (seconds) between direct mill API polls by a paused periodic monitor in its wait loop. Each paused monitor polls the mill for its tracked ticket's state at this interval; when the state differs from the checkpoint's `last_known_state` the monitor resumes immediately (zero added latency). The background watcher's `paused_monitor_poll_interval_seconds` serves as a safety-net backup. Set to `0` to disable per-monitor long-polling (watcher-only wake).                                 |
| `subsessions.paused_monitor_auto_resume_seconds`        | `number`  | `1800.0`                   | Maximum wall-clock seconds a paused periodic monitor remains paused before auto-resuming regardless of ticket-state changes. When a monitor has been paused for longer than this interval (e.g. 1800 s = 30 min), it resumes its normal periodic cycle so the operator does not need to manually intervene. Set to `0` to disable time-based auto-resume (monitor stays paused until a state change or manual message arrives).                                                                              |
| `subsessions.paused_monitor_max_reblock_resumes`        | `integer` | `3`                        | Maximum number of consecutive BLOCKED-on-resume events before a paused periodic monitor is closed with reason `repeated_blocked`. When a ticket is BLOCKED on every resume (the agent keeps hitting the same failure without making progress), auto-retry is futile — the monitor is closed so the operator can intervene. Set to `0` to disable auto-close on repeated blocks (the monitor will keep retrying indefinitely).                                                                                |
| `subsessions.paused_monitor_reblock_notify_threshold`   | `integer` | `2`                        | Number of consecutive BLOCKED-on-resume events before an SSE notification is sent to the parent conversation alerting the operator that the monitor is re-blocking. This surfaces silent auto-resume→re-block loops so the operator can decide whether to rebase the branch, revert problematic files, or take other action before the `paused_monitor_max_reblock_resumes` cap is reached. Set to `0` to disable notifications.                                                                             |
| `subsessions.periodic_max_interval_seconds`             | `number`  | `3600.0`                   | Upper bound (seconds) for a periodic subsession's self-adjusted interval. The `adjust_periodic_interval` tool clamps to this value. Default 3600 (1 hour).                                                                                                                                                                                                                                                                                                                                                   |
| `subsessions.periodic_max_total_runs`                   | `integer` | `100`                      | Upper bound for a periodic subsession's self-adjusted `max_runs` (total run budget). The `adjust_periodic_budget` tool clamps to this value. Default 100.                                                                                                                                                                                                                                                                                                                                                    |
| `subsessions.transient_error_max_retries`               | `integer` | `3`                        | Maximum retry attempts when a periodic subsession's agent turn fails with a transient API error (e.g. OpenRouter upstream hiccup). Retries use `robotsix_http`'s exponential backoff with jitter; the delays are not operator-configurable, only the attempt count. When retries are exhausted the cycle is skipped and the schedule continues rather than permanently failing the subsession.                                                                                                               |
| `subsessions.max_runs_escalation_threshold`             | `integer` | `3`                        | Number of consecutive times a periodic subsession can hit its `max_runs` limit before auto-escalating with a follow-up ticket. When the threshold is reached, a follow-up ticket is created on the board and the monitor closes with reason `max_runs_escalated`. Set to `0` to disable escalation.                                                                                                                                                                                                          |
| `subsessions.max_runs_progress_extension`               | `integer` | `20`                       | Number of additional runs granted to a periodic subsession when it reaches its `max_runs` cap but has observed progress within the recent window (a non-`NO_CHANGE`, non-duplicate reply). The extended cap is clamped by `periodic_max_total_runs`. Set to `0` to disable adaptive extension.                                                                                                                                                                                                               |
| `subsessions.max_runs_progress_window`                  | `integer` | `5`                        | Number of recent runs inspected for progress when a periodic subsession reaches its `max_runs` cap. A run counts as progress when the agent replies with something other than `NO_CHANGE` and other than a verbatim duplicate of the prior reply. Set to `0` to disable adaptive extension.                                                                                                                                                                                                                  |
| `subsessions.monitor_slot_budget`                       | `integer` | `8`                        | Maximum number of occupied monitor slots per conversation (active + paused periodic monitors). When a conversation reaches this budget, new monitor requests first try to reuse the least-recently-active paused monitor; if none is paused the request is queued rather than evicting a live monitor. Set to `0` to disable per-conversation slot budgeting (all spawns proceed immediately).                                                                                                               |
| `subsessions.monitor_slot_queue_max`                    | `integer` | `32`                       | Maximum number of pending monitor-spawn requests queued per conversation when the slot budget is exhausted and no paused monitor is available for reuse. A request that would exceed this limit is rejected with a clear error instead of growing the queue unbounded.                                                                                                                                                                                                                                       |
| `subsessions.image_publish_workflow_name`               | `string`  | `"release-image.yml"`      | Filename of the image-publish workflow in the monitored repo's `.github/workflows/` directory. After a tracked PR is merged, the watcher checks the most recent run of this workflow on the repo's default branch to verify the image was successfully published before resuming the monitor. When the latest run failed or is still in progress, the watcher keeps the monitor paused and emits a notification. Set to an empty string to disable post-merge image-publish verification (legacy behaviour). |
| `subsessions.image_publish_verify_timeout_seconds`      | `number`  | `1800.0`                   | Maximum wall-clock seconds the watcher waits for the image-publish workflow to complete after a tracked PR is merged. When the latest run is still in progress and this timeout has not elapsed, the watcher keeps the monitor paused and retries on the next poll cycle. When the timeout elapses without a successful run, the watcher resumes the monitor with a warning so the agent can investigate. Default 1800 s (30 min).                                                                           |
| `subsessions.user_chat_max_retries`                     | `integer` | `3`                        | Maximum automatic retries for `user_chat` and `task` subsession failures. Each retry re-launches the subsession with the prior error folded into the prompt so the agent can self-correct. Once exhausted the subsession is failed and, for `user_chat`, the original decision prompt is surfaced in the main conversation as a fallback so the operator can answer directly.                                                                                                                                |
| `subsessions.monitor_error_max_retries`                 | `integer` | `2`                        | Maximum automatic retries for `periodic` and `wait_for_event` monitor subsessions that fail with a non-transient error (e.g. tool retry limit, unexpected exception). Each retry re-launches the subsession worker with a system note about the prior failure so the agent can self-correct. Once exhausted the monitor is permanently failed and the parent is notified. Set to `0` to disable monitor error retries (monitors fail on the first error).                                                    |
| `subsessions.turn_budget.task.soft_warn_turns`          | `integer` | `25`                       | Number of agent turns before a `task` subsession receives a system reminder to wrap up and call `complete_subsession`. Set to `0` to disable the soft-warn for `task` subsessions.                                                                                                                                                                                                                                                                                                                           |
| `subsessions.turn_budget.task.hard_stop_turns`          | `integer` | `40`                       | Number of agent turns before a `task` subsession is force-closed with a partial-work summary. Set to `0` to disable the hard-stop for `task` subsessions.                                                                                                                                                                                                                                                                                                                                                    |
| `subsessions.turn_budget.periodic.soft_warn_turns`      | `integer` | `0`                        | Same as `task.soft_warn_turns` for `periodic` and `wait_for_event` subsessions. Defaults to `0` (disabled) — monitors are already bounded by `monitor_max_model_level`, `run_timeout_seconds`, and `periodic_max_total_runs`, and are designed to stay alive for the whole life of a ticket.                                                                                                                                                                                                                 |
| `subsessions.turn_budget.periodic.hard_stop_turns`      | `integer` | `0`                        | Same as `task.hard_stop_turns` for `periodic` and `wait_for_event` subsessions. Defaults to `0` (disabled) — monitors are already bounded by `monitor_max_model_level`, `run_timeout_seconds`, and `periodic_max_total_runs`, and are designed to stay alive for the whole life of a ticket.                                                                                                                                                                                                                 |
| `subsessions.turn_budget.user_chat.soft_warn_turns`     | `integer` | `25`                       | Same as `task.soft_warn_turns` for `user_chat` subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `subsessions.turn_budget.user_chat.hard_stop_turns`     | `integer` | `40`                       | Same as `task.hard_stop_turns` for `user_chat` subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `subsessions.turn_budget.on_close.soft_warn_turns`      | `integer` | `25`                       | Same as `task.soft_warn_turns` for `on_close` subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `subsessions.turn_budget.on_close.hard_stop_turns`      | `integer` | `40`                       | Same as `task.hard_stop_turns` for `on_close` subsessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

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
| `feedback.timeout`              | `number`          | `60.0`  | Per-request HTTP timeout (seconds) for ingest calls.                       |
| `feedback.max_tickets_per_run`  | `integer`         | `3`     | Ceiling on tickets filed by one feedback run. `0` disables filing.         |
| `feedback.dedup_window_seconds` | `number`          | `60.0`  | Seconds to suppress duplicate runs (per session) and duplicate titles.     |
| `feedback.ingest_max_retries`   | `integer`         | `2`     | Idempotent retries when an ingest POST hits a transport error/timeout.     |

**Deduplication.** Two guards prevent near-simultaneous feedback runs from filing duplicate tickets:

1. **Session-level debounce** — when a feedback run is scheduled for a session that already had a
   run start within `dedup_window_seconds`, the new run is skipped. This prevents multiple
   compactions or session-end triggers (from concurrent subsessions) from spawning overlapping
   analyses of the same transcript.
1. **Title-level dedup** — before POSTing a ticket, the runner checks whether a ticket with the same
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
1. **Mill repo registry** — `GET http://mill:8077/repos` fetches the list of registered repos from
   the mill board.
1. **Intersection** — only repos present in *both* the deploy roster *and* the mill repo registry
   are allowed. A repo that is registered but not deployed (or vice versa) cannot receive tickets.
1. **Fallback** — if either service is unreachable, returns an empty response, or the intersection
   is empty, the runner falls back to `["robotsix-chat"]` and logs a warning so the feedback
   pipeline continues to function in a degraded state.

The `central_deploy.deploy_api_key` config field (see the Central Deploy table above) supplies the
`X-API-Key` header for the central-deploy API; it is needed only when the deploy server requires
authentication. There is no environment-variable equivalent — per the config standard's
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

| JSON key                     | Type      | Default             | Description                                        |
| ---------------------------- | --------- | ------------------- | -------------------------------------------------- |
| `github_security.enabled`    | `boolean` | `false`             | Master switch.                                     |
| `github_security.github_org` | `string`  | `"damien-robotsix"` | GitHub organisation name whose repos are in scope. |

### GitHub Actions

GitHub Actions secrets and workflow dispatch via the GitHub App installation. Disabled by default.

| JSON key                    | Type      | Default             | Description                                        |
| --------------------------- | --------- | ------------------- | -------------------------------------------------- |
| `github_actions.enabled`    | `boolean` | `false`             | Master switch.                                     |
| `github_actions.github_org` | `string`  | `"damien-robotsix"` | GitHub organisation name whose repos are in scope. |

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

| JSON key                             | Type              | Default  | Description                                                                                                                                                    |
| ------------------------------------ | ----------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lifecycle.enabled`                  | `boolean`         | `false`  | Master switch.                                                                                                                                                 |
| `lifecycle.default_protocol`         | `string`          | `"http"` | Protocol scheme prepended when `central_deploy.url` lacks one (e.g. `"https"` for TLS). Ignored when the URL already has a recognised scheme (`http`/`https`). |
| `lifecycle.api_key`                  | `string` (secret) | `""`     | Optional API key for the deploy-lifecycle API.                                                                                                                 |
| `lifecycle.service_name`             | `string`          | `""`     | Name of this service as registered with the deploy server.                                                                                                     |
| `lifecycle.timeout`                  | `number`          | `30.0`   | Per-request HTTP timeout (seconds).                                                                                                                            |
| `lifecycle.self_restart_max_retries` | `integer`         | `3`      | Maximum number of retries for transient `self_restart` failures (5xx, timeouts, connection errors). 0 = no retries.                                            |

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

### Health

Periodic health-check settings. When enabled, a background scheduler runs every
`health.check_interval_seconds` (default 300 s / 5 min) and verifies that critical subsystems are
reachable and producing expected output: memory (cognee recall), knowledge store, feedback runner,
and diagnostics store. Results are exposed via `GET /health` and logged.

| JSON key                        | Type      | Default | Description                                                |
| ------------------------------- | --------- | ------- | ---------------------------------------------------------- |
| `health.enabled`                | `boolean` | `true`  | Master switch. When `false`, no health checks run.         |
| `health.check_interval_seconds` | `number`  | `300.0` | Seconds between scheduled health-check cycles. Must be >0. |

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

### Periodic sessions

Periodic sessions are **ordinary chat sessions started on a schedule**. Each preset fires on its
interval: the scheduler creates a fresh session under the `periodic` owner and posts the preset's
`initial_prompt` through the same code path as an operator message. There is no execution state
machine, no self-scheduled continuation, and no restart-resume — see
[the user guide](user-guide/periodic-sessions.md).

| JSON key                                    | Type      | Default | Description                                                                                                             |
| ------------------------------------------- | --------- | ------- | ----------------------------------------------------------------------------------------------------------------------- |
| `periodic.sessions`                         | `array`   | `[]`    | Named periodic session presets (see below). An empty list means nothing fires.                                          |
| `periodic.ready_staleness_minutes`          | `integer` | `10`    | Minutes a ticket can remain `ready` before `list_stale_ready_tickets` surfaces it as stale.                             |
| `periodic.priority_ready_staleness_minutes` | `integer` | `60`    | The same threshold for priority-flagged tickets (longer: priority tickets often wait in a serial implementation queue). |

Each entry in `periodic.sessions` is a `PeriodicSessionDefinition` object:

| JSON key                    | Type      | Default | Description                                                                                                                           |
| --------------------------- | --------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                      | `string`  | —       | Unique preset name; also used in session titles (`<name> — <date>`).                                                                  |
| `initial_prompt`            | `string`  | `""`    | The one message the scheduler posts into the fresh session. Write it as a complete task brief: task, scope, hard constraints, report. |
| `schedule_interval_seconds` | `number`  | `86400` | Spacing between firings (min 300). A never-fired preset fires promptly after startup.                                                 |
| `model_level`               | `integer` | `null`  | Optional llmio level (1–3) for this preset's sessions. `null` follows the global model-level resolution, like an operator session.    |
| `enabled`                   | `boolean` | `true`  | When `false`, the preset never fires.                                                                                                 |

Endpoints:

- `GET /periodic/definitions` — presets with their firing state (`last_fired_at`, `last_session_id`,
  `runs`).
- `POST /periodic/definitions/{name}/run` — fire a preset now (409 while its previous session is
  mid-turn).

**Example** — a daily read-only mail review:

```json
"periodic": {
  "sessions": [
    {
      "name": "mail-triage",
      "initial_prompt": "Review today's mail triage decisions. READ-ONLY: never move, archive, delete, or send anything. Finish with a concise report of findings.",
      "schedule_interval_seconds": 86400
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

### File Hub Tools

File-hub integration — fetch, fill, render, and upload documents via the robotsix-file-hub service.
When enabled, the agent gains five tools: `file_hub_get` (download by id), `list_pdf_form_fields`
(inspect AcroForm fields), `render_pdf_page` (render a PDF page to a viewable image),
`fill_pdf_document` (set form fields or overlay text), and `file_hub_put` (upload a local file).
Disabled by default.

| JSON key                            | Type      | Default                  | Description                                                                  |
| ----------------------------------- | --------- | ------------------------ | ---------------------------------------------------------------------------- |
| `file_hub_tools.enabled`            | `boolean` | `false`                  | Master switch. When `false`, no file-hub tools are registered.               |
| `file_hub_tools.base_url`           | `string`  | `"http://file-hub:8080"` | Base URL of the file-hub service. Must be reachable from the chat container. |
| `file_hub_tools.working_dir`        | `string`  | `"/data/file_hub_work"`  | Local directory for downloaded and filled files.                             |
| `file_hub_tools.max_download_bytes` | `integer` | `52428800`               | Maximum file size in bytes for downloads (default 50 MB). Must be >0.        |
| `file_hub_tools.timeout`            | `number`  | `60.0`                   | Per-request HTTP timeout in seconds. Must be >0.                             |

### Volume Tools

Local volume-directory listing. When enabled, the agent gains a `list_volume_files` tool that
returns the contents of a directory under the configured root path — a read-only,
local-filesystem-only primitive with no remote access and no write capability. Enabled by default.

| JSON key                 | Type      | Default   | Description                                                                   |
| ------------------------ | --------- | --------- | ----------------------------------------------------------------------------- |
| `volume_tools.enabled`   | `boolean` | `true`    | Master switch. When `false`, no `list_volume_files` tool is offered.          |
| `volume_tools.root_path` | `string`  | `"/data"` | Root directory for volume file listings. Paths outside this root are refused. |

### Mobile Auth (SSO)

Mobile SSO authentication via tinyauth reverse proxy. When enabled, exposes `GET /auth/login` and
`POST /chat/auth/mobile-token` for the mobile app's authentication flow. Disabled by default.

> **Deployment note:** The auth endpoints (`GET /auth/login`, `POST /chat/auth/mobile-token`) are
> only served when **both** conditions hold:
>
> 1. The chat backend image running in production contains this implementation (i.e. the container
>    was **redeployed** from a build that includes the auth routes), **and**
> 1. `mobile_auth.enabled` is set to `true` in the production config file.
>
> When `enabled` is `false` (the default) — or when `mobile_auth` is absent from the config — the
> endpoints deliberately return `404` (the routes exist but are gated off). A green CI run proves
> the code compiles and unit tests pass; it does **not** prove the endpoints are live. Always verify
> with a live HTTP probe against the deployed backend after enabling.

| JSON key                               | Type              | Default                 | Description                                                                                                                  |
| -------------------------------------- | ----------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `mobile_auth.enabled`                  | `boolean`         | `false`                 | Master switch. When `false`, the auth endpoints return 404.                                                                  |
| `mobile_auth.tinyauth_url`             | `string`          | `""`                    | Base URL of the tinyauth instance (e.g. `"https://auth.robotsix.net"`).                                                      |
| `mobile_auth.subject_header`           | `string`          | `"X-Forwarded-User"`    | HTTP header where tinyauth writes the authenticated user identity.                                                           |
| `mobile_auth.session_header`           | `string`          | `"X-Forwarded-Session"` | HTTP header where tinyauth writes the session identifier.                                                                    |
| `mobile_auth.token_secret`             | `string` (secret) | `""`                    | HMAC secret used to sign the short-lived bearer tokens. Must be set when `enabled` is `true`.                                |
| `mobile_auth.token_ttl_seconds`        | `integer`         | `3600`                  | Bearer token lifetime in seconds. Must be > 0.                                                                               |
| `mobile_auth.allowed_redirect_domains` | `array[string]`   | `[]`                    | Allowlist of domains for the `redirect_to` query parameter in `GET /auth/login`. Must be non-empty when `enabled` is `true`. |
| `mobile_auth.callback_base_url`        | `string`          | `""`                    | Public base URL of this chat server (e.g. `"https://chat.robotsix.net"`), used to construct the tinyauth callback URL.       |

**Required values when `enabled` is `true`** — the server will not provide a functional handshake
unless the following are set: `tinyauth_url`, `token_secret`, `allowed_redirect_domains`
(non-empty), and `callback_base_url`.

**Live verification** — the canonical check that the endpoints are live and behaving per contract:

```bash
# GET /auth/login should NOT be 404 once enabled
curl -s -o /dev/null -w '%{http_code}\n' 'https://chat.robotsix.net/auth/login?redirect_to=<allowed-app-domain>'

# POST /chat/auth/mobile-token — missing identity header should be 401 (not 404)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://chat.robotsix.net/chat/auth/mobile-token

# POST /chat/auth/mobile-token — with a tinyauth identity header set by the edge proxy
curl -s -X POST -H 'X-Forwarded-User: <user>' https://chat.robotsix.net/chat/auth/mobile-token
```

The identity for `POST /chat/auth/mobile-token` is always taken from the configured `subject_header`
(default `X-Forwarded-User`), which only the trusted tinyauth reverse proxy can set — never from the
request body.

## Schema

The committed `config/config.schema.json` is the authoritative schema for the `Settings` model. It
is generated by the shared `robotsix-config` CLI — the same helper every fleet component uses, so
the committed file has one canonical formatting — and **CI-checked** to stay in sync: the check
regenerates from the model and fails the build with a unified diff on any drift.

To regenerate locally:

```bash
uv run robotsix-config schema robotsix_chat.config.Settings
```

To check without writing (what CI runs):

```bash
uv run robotsix-config schema --check robotsix_chat.config.Settings
```

Note the model path is fully dot-separated (`module.Class`), not `module:Class`.
