# Configuration

robotsix-chat is configured via a single JSON config file, loaded by
[`robotsix-config`](https://github.com/damien-robotsix/robotsix-config). There is no YAML cascade
and no env-var overlay — the only environment variable consumed for config is the file locator.

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
- `langfuse.public_key`, `langfuse.secret_key`
- `memory.llm.api_key`, `memory.embedding.api_key`
- `memory.langfuse.public_key`, `memory.langfuse.secret_key`
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

| JSON key                    | Type              | Default                                               | Description                                                                                                                                                                                                                                                                                                                                                                     |
| --------------------------- | ----------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `llmio_model_level`         | `integer`         | `3`                                                   | LLM capability level: `1` (cheapest), `2`, `3`, or `4` (best).                                                                                                                                                                                                                                                                                                                  |
| `llmio_api_key`             | `string` (secret) | `""`                                                  | OpenRouter API key. Required for levels 1–2; ignored for 3–4.                                                                                                                                                                                                                                                                                                                   |
| `summary_model_level`       | `integer`         | `1`                                                   | LLM capability level used to regenerate `POST /summary`'s structured extraction after each turn.                                                                                                                                                                                                                                                                                |
| `agent_instruction`         | `string`          | (long default)                                        | System instruction for the agent. Governed by the code default in `src/robotsix_chat/config/settings.py` (currently v45). Intentionally absent from `config/config.json` — the code default is the single source of truth. Operators who need to override it can add `"agent_instruction"` to their local or deployed config file; doing so bypasses the code default entirely. |
| `max_images_per_message`    | `integer`         | `8`                                                   | Maximum images per chat message.                                                                                                                                                                                                                                                                                                                                                |
| `max_image_bytes`           | `integer`         | `5242880`                                             | Maximum image size in bytes (5 MiB).                                                                                                                                                                                                                                                                                                                                            |
| `allowed_image_media_types` | `array[string]`   | `["image/png","image/jpeg","image/gif","image/webp"]` | Allowed image MIME types.                                                                                                                                                                                                                                                                                                                                                       |

### Server

| JSON key                | Type            | Default          | Description                                          |
| ----------------------- | --------------- | ---------------- | ---------------------------------------------------- |
| `server_host`           | `string`        | `"0.0.0.0"`      | Host the server binds to.                            |
| `server_port`           | `integer`       | `8000`           | Port the server listens on.                          |
| `idle_timeout_minutes`  | `integer`       | `30`             | Minutes of inactivity before closing the connection. |
| `log_level`             | `string`        | `"INFO"`         | Python logging level.                                |
| `cors_allow_origins`    | `array[string]` | `[]`             | Origins allowed to call `/chat` cross-origin.        |
| `correlation_id_header` | `string`        | `"X-Request-ID"` | Header name for request correlation ids.             |

### Langfuse (tracing)

| JSON key              | Type              | Default                        | Description          |
| --------------------- | ----------------- | ------------------------------ | -------------------- |
| `langfuse.public_key` | `string` (secret) | `""`                           | Langfuse public key. |
| `langfuse.secret_key` | `string` (secret) | `""`                           | Langfuse secret key. |
| `langfuse.host`       | `string`          | `"https://cloud.langfuse.com"` | Langfuse host.       |

These keys trace the main chat agent. When memory is enabled, a **separate** Langfuse project
(`memory.langfuse.*`) traces the cognee/LiteLLM pipeline independently — see
[Memory](#memory-cognee).

### Memory (cognee)

Persistent, cross-conversation episodic memory via embedded cognee. Disabled by default.

| JSON key                                 | Type              | Default                                   | Description                                                                                                                                                                                                               |
| ---------------------------------------- | ----------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.enabled`                         | `boolean`         | `false`                                   | Master switch. Requires cognee extras.                                                                                                                                                                                    |
| `memory.data_dir`                        | `string`          | `"/data/cognee"`                          | Cognee store directory (keep on persistent volume).                                                                                                                                                                       |
| `memory.recall_search_type`              | `string`          | `"GRAPH_COMPLETION"`                      | Cognee recall search type.                                                                                                                                                                                                |
| `memory.recall_timeout_seconds`          | `number`          | `60.0`                                    | Hard timeout (seconds) for a single `recall` call. On expiry degrades to empty string — the agent proceeds without memory.                                                                                                |
| `memory.remember_timeout_seconds`        | `number`          | `300.0`                                   | Hard timeout (seconds) for a single `remember` call (cognify consolidation). On expiry the write is skipped and a warning is logged.                                                                                      |
| `memory.write_backlog_path`              | `string`          | `"/data/cognee/backlog.jsonl"`            | Path to a durable JSONL backlog for exchanges that could not be persisted after retries are exhausted. Drained opportunistically on subsequent successful writes.                                                         |
| `memory.datafusion_runtime_memory_limit` | `string`          | `"256M"`                                  | DataFusion memory-pool limit (e.g. `"256M"`, `"1G"`). Bounds the LanceDB worker subprocess memory so a single large `merge_insert` does not OOM the container. Safe default for 2 GB containers; raise for larger limits. |
| `memory.frozen_store_alert_minutes`      | `number`          | `10.0`                                    | Duration (minutes) of consecutive write failures before a `WARNING` diagnostic is emitted — prevents a silently frozen vector store from going unnoticed for days.                                                        |
| `memory.write_throttle_seconds`          | `number`          | `0.5`                                     | Delay (seconds) between serialised writes so the LanceDB worker subprocess can complete each `merge_insert` before the next starts. Prevents burst OOM.                                                                   |
| `memory.llm.provider`                    | `string`          | `"custom"`                                | Extraction LLM provider.                                                                                                                                                                                                  |
| `memory.llm.model`                       | `string`          | `"openrouter/deepseek/deepseek-v4-flash"` | Extraction LLM model.                                                                                                                                                                                                     |
| `memory.llm.endpoint`                    | `string`          | `"https://openrouter.ai/api/v1"`          | Extraction LLM endpoint.                                                                                                                                                                                                  |
| `memory.llm.api_key`                     | `string` (secret) | `""`                                      | OpenRouter API key for extraction.                                                                                                                                                                                        |
| `memory.embedding.provider`              | `string`          | `"openai_compatible"`                     | Embedding provider.                                                                                                                                                                                                       |
| `memory.embedding.model`                 | `string`          | `"bge-m3"`                                | Embedding model name.                                                                                                                                                                                                     |
| `memory.embedding.endpoint`              | `string`          | `""`                                      | Embedding server URL (e.g. `http://host:11434/v1`).                                                                                                                                                                       |
| `memory.embedding.dimensions`            | `integer`         | `1024`                                    | Embedding vector dimensions.                                                                                                                                                                                              |
| `memory.embedding.api_key`               | `string` (secret) | `""`                                      | Bearer token for the embedding server.                                                                                                                                                                                    |
| `memory.embedding.huggingface_tokenizer` | `string`          | `"BAAI/bge-m3"`                           | HuggingFace tokenizer name.                                                                                                                                                                                               |
| `memory.langfuse.public_key`             | `string` (secret) | `""`                                      | Langfuse public key (robotsix-chat-cognee project).                                                                                                                                                                       |
| `memory.langfuse.secret_key`             | `string` (secret) | `""`                                      | Langfuse secret key (robotsix-chat-cognee project).                                                                                                                                                                       |
| `memory.langfuse.host`                   | `string`          | `"https://cloud.langfuse.com"`            | Langfuse host for cognee tracing.                                                                                                                                                                                         |

### Central Deploy

Component-access roster and skill loading from the central-deploy management plane.

| JSON key                                      | Type              | Default  | Description                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------- | ----------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `central_deploy.url`                          | `string`          | `""`     | Base URL of the central-deploy API (no trailing slash).                                                                                                                                                                                                                                                                                                           |
| `central_deploy.api_token`                    | `string` (secret) | `""`     | Bearer token for the central-deploy API.                                                                                                                                                                                                                                                                                                                          |
| `central_deploy.roster_cache_ttl`             | `number`          | `300.0`  | Seconds to cache the component roster before re-fetching.                                                                                                                                                                                                                                                                                                         |
| `central_deploy.component_response_max_chars` | `integer`         | `200000` | Default truncation limit for GET/HEAD component responses — write methods keep the 8,000-char limit. Raised from 8,000 so large ticket lists (e.g. mill board blocked tickets) enumerate fully. Each call can override this with `component_request`'s `max_response_chars` parameter (e.g. `max_response_chars=2000` for a compact summary of a ticket history). |

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

| JSON key                               | Type      | Default                    | Description                                                                                                                 |
| -------------------------------------- | --------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `subsessions.max_concurrent`           | `integer` | `8`                        | Maximum concurrent subsessions.                                                                                             |
| `subsessions.max_depth`                | `integer` | `3`                        | Maximum nesting depth.                                                                                                      |
| `subsessions.default_model_level`      | `integer` | `2`                        | Default model level for spawned subsessions.                                                                                |
| `subsessions.min_interval_seconds`     | `number`  | `60.0`                     | Minimum interval between periodic runs.                                                                                     |
| `subsessions.auto_stop_no_change_runs` | `integer` | `5`                        | Consecutive NO_CHANGE runs before auto-stop.                                                                                |
| `subsessions.run_timeout_seconds`      | `number`  | `600.0`                    | Hard per-run timeout (seconds) for a single subsession turn. On expiry the run is marked failed and the schedule continues. |
| `subsessions.store_path`               | `string`  | `"/data/subsessions.json`" | Path to the subsession persistence file.                                                                                    |
| `subsessions.transcript_max_entries`   | `integer` | `200`                      | Maximum transcript entries per subsession.                                                                                  |

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

The `DEPLOY_API_KEY` environment variable may be set to pass an `X-API-Key` header to the
central-deploy API (optional; needed only when the deploy server requires authentication).

### Direct Repo (GitHub App)

Push-branch and open-PR as the robotsix-mill GitHub App. Disabled by default.

| JSON key                                 | Type              | Default                    | Description                                           |
| ---------------------------------------- | ----------------- | -------------------------- | ----------------------------------------------------- |
| `direct_repo.enabled`                    | `boolean`         | `false`                    | Master switch.                                        |
| `direct_repo.github_app_id`              | `string`          | `""`                       | GitHub App numeric or slug id. Required when enabled. |
| `direct_repo.github_app_private_key`     | `string` (secret) | `""`                       | RSA private key in PEM format.                        |
| `direct_repo.github_app_installation_id` | `string`          | `""`                       | Installation id to act as.                            |
| `direct_repo.github_api_base_url`        | `string`          | `"https://api.github.com"` | Base URL for GitHub Enterprise.                       |
| `direct_repo.board_api_base_url`         | `string`          | `"http://127.0.0.1:8077"`  | Board HTTP API base URL for ticket-state lookups.     |
| `direct_repo.board_api_token`            | `string` (secret) | `""`                       | Optional bearer token for the board API.              |
| `direct_repo.timeout`                    | `number`          | `30.0`                     | Per-request HTTP timeout (seconds).                   |

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

### Render URL

Read-only URL rendering via headless Chromium (Playwright). Loads a URL in a headless browser,
captures a full-page screenshot (base64-encoded PNG), and extracts the accessibility tree — both
returned to the agent as structured JSON for UI verification. No interactive browsing, form-filling,
or navigation beyond the initial page load is permitted. Requires the `render-url` extra
(`playwright`) in the image as well as a Playwright Chromium browser installation. Disabled by
default.

| JSON key                     | Type      | Default | Description                              |
| ---------------------------- | --------- | ------- | ---------------------------------------- |
| `render_url.enabled`         | `boolean` | `false` | Master switch.                           |
| `render_url.timeout`         | `number`  | `30.0`  | Per-request page-load timeout (seconds). |
| `render_url.viewport_width`  | `integer` | `1280`  | Browser viewport width (pixels).         |
| `render_url.viewport_height` | `integer` | `720`   | Browser viewport height (pixels).        |

______________________________________________________________________

## Schema

The committed `config/config.schema.json` is the authoritative schema for the `Settings` model. It
is auto-generated from the pydantic model via `Settings.model_json_schema()` and **CI-checked** to
stay in sync — a CI job regenerates it from the model and fails the build on any drift.

To regenerate locally:

```bash
python -c 'import json; from robotsix_chat.config import Settings; print(json.dumps(Settings.model_json_schema(), indent=2))' > config/config.schema.json
```
