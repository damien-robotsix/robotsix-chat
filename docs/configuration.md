# Configuration

robotsix-chat is configured via a single JSON config file, loaded by
[`robotsix-config`](https://github.com/damien-robotsix/robotsix-config). There is no YAML cascade
and no env-var overlay — the only environment variable consumed for config is the file locator.

## Config file

The JSON file lives at **`config/config.json`** by default. Its path is set by the
`ROBOTSIX_CONFIG_FILE` environment variable.

**Getting started:**


```bash
cp config/config.example.json config/config.json
# Edit config/config.json — fill in secrets for the features you enable.
```

- `config/config.json` is **gitignored** — credentials never land in the repo.
- `config/config.example.json` is **committed** — it documents every field with its default value.
- `config/config.schema.json` is **committed and CI-checked** — the CI pipeline regenerates it from
  the `Settings` pydantic model and fails on any drift, so the schema always reflects the live code.

## Local dev

- The app starts with **defaults only** when no config file exists — non-secret features (server,
  knowledge, diagnostics) work out of the box.
- Create `config/config.json` and set `ROBOTSIX_CONFIG_FILE` when you need secrets (API keys, broker
  tokens) or want to override defaults.

## Secrets

Fields of JSON Schema type `string` with `writeOnly: true` are treated as secrets (`SecretStr`).
They are never logged, never serialized in diagnostics or trace output, and are redacted in stack
traces.

Secret fields include:

- `llmio_api_key`
- `langfuse.public_key`, `langfuse.secret_key`
- `memory.llm.api_key`, `memory.embedding.api_key`
- `memory.langfuse.public_key`, `memory.langfuse.secret_key`
- `mill.broker_token`
- `mail.api_token`
- `calendar.broker_token`
- `component_agent.broker_token`
- `board_reader.api_token`
- `direct_repo.github_app_private_key`, `direct_repo.board_api_token`
- `refdocs.github_token`
- `version_check.github_token`

## Settings reference

All fields and their defaults are listed in `config/config.example.json`. The sections below
describe each group.

______________________________________________________________________

### Top-level

| JSON key                    | Type              | Default                                               | Description                                                    |
| --------------------------- | ----------------- | ----------------------------------------------------- | -------------------------------------------------------------- |
| `llmio_model_level`         | `integer`         | `3`                                                   | LLM capability level: `1` (cheapest), `2`, `3`, or `4` (best). |
| `llmio_api_key`             | `string` (secret) | `""`                                                  | OpenRouter API key. Required for levels 1–2; ignored for 3–4.  |
| `agent_instruction`         | `string`          | (long default)                                        | System instruction for the agent.                              |
| `max_images_per_message`    | `integer`         | `8`                                                   | Maximum images per chat message.                               |
| `max_image_bytes`           | `integer`         | `5242880`                                             | Maximum image size in bytes (5 MiB).                           |
| `allowed_image_media_types` | `array[string]`   | `["image/png","image/jpeg","image/gif","image/webp"]` | Allowed image MIME types.                                      |

### Server

| JSON key                | Type            | Default          | Description                                          |
| ----------------------- | --------------- | ---------------- | ---------------------------------------------------- |
| `server_host`           | `string`        | `"127.0.0.1"`    | Host the server binds to.                            |
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

| JSON key                                 | Type              | Default                                   | Description                                         |
| ---------------------------------------- | ----------------- | ----------------------------------------- | --------------------------------------------------- |
| `memory.enabled`                         | `boolean`         | `false`                                   | Master switch. Requires cognee extras.              |
| `memory.data_dir`                        | `string`          | `"/data/cognee"`                          | Cognee store directory (keep on persistent volume). |
| `memory.recall_search_type`              | `string`          | `"GRAPH_COMPLETION"`                      | Cognee recall search type.                          |
| `memory.llm.provider`                    | `string`          | `"custom"`                                | Extraction LLM provider.                            |
| `memory.llm.model`                       | `string`          | `"openrouter/deepseek/deepseek-v4-flash"` | Extraction LLM model.                               |
| `memory.llm.endpoint`                    | `string`          | `"https://openrouter.ai/api/v1"`          | Extraction LLM endpoint.                            |
| `memory.llm.api_key`                     | `string` (secret) | `""`                                      | OpenRouter API key for extraction.                  |
| `memory.embedding.provider`              | `string`          | `"openai_compatible"`                     | Embedding provider.                                 |
| `memory.embedding.model`                 | `string`          | `"bge-m3"`                                | Embedding model name.                               |
| `memory.embedding.endpoint`              | `string`          | `""`                                      | Embedding server URL (e.g. `http://host:11434/v1`). |
| `memory.embedding.dimensions`            | `integer`         | `1024`                                    | Embedding vector dimensions.                        |
| `memory.embedding.api_key`               | `string` (secret) | `""`                                      | Bearer token for the embedding server.              |
| `memory.embedding.huggingface_tokenizer` | `string`          | `"BAAI/bge-m3"`                           | HuggingFace tokenizer name.                         |
| `memory.langfuse.public_key`             | `string` (secret) | `""`                                      | Langfuse public key (robotsix-chat-cognee project). |
| `memory.langfuse.secret_key`             | `string` (secret) | `""`                                      | Langfuse secret key (robotsix-chat-cognee project). |
| `memory.langfuse.host`                   | `string`          | `"https://cloud.langfuse.com"`            | Langfuse host for cognee tracing.                   |

### Mill (broker integration)

Agent-comm broker integration with the robotsix-mill board manager. Disabled by default.

| JSON key                | Type              | Default                         | Description                                              |
| ----------------------- | ----------------- | ------------------------------- | -------------------------------------------------------- |
| `mill.enabled`          | `boolean`         | `false`                         | Master switch. Requires the `broker` extra.              |
| `mill.broker_host`      | `string`          | `"ai-broker.robotsix.net"`      | Broker hostname.                                         |
| `mill.broker_port`      | `integer`         | `443`                           | Broker port.                                             |
| `mill.broker_scheme`    | `string`          | `"https"`                       | `https` (TLS) or `http`.                                 |
| `mill.broker_token`     | `string` (secret) | `""`                            | Agent bearer token registered on the broker.             |
| `mill.agent_id`         | `string`          | `"robotsix-chat"`               | This agent's id on the broker.                           |
| `mill.board_manager_id` | `string`          | `"board-manager-robotsix-mill"` | Recipient board manager agent id.                        |
| `mill.repo_id`          | `string`          | `""`                            | Override target repo; empty means board-manager decides. |
| `mill.timeout`          | `number`          | `600.0`                         | Per-request timeout (seconds).                           |

### Mail (board HTTP read)

Direct HTTP access to the mill's board API for listing, reading, and creating tickets.

| JSON key            | Type              | Default                   | Description                                         |
| ------------------- | ----------------- | ------------------------- | --------------------------------------------------- |
| `mail.enabled`      | `boolean`         | `false`                   | Master switch.                                      |
| `mail.api_base_url` | `string`          | `"http://127.0.0.1:8077"` | Base URL of the board HTTP API (no trailing slash). |
| `mail.api_token`    | `string` (secret) | `""`                      | Optional bearer token for the board API.            |
| `mail.timeout`      | `number`          | `30.0`                    | Per-request HTTP timeout (seconds).                 |

> **Note:** This section was previously called "Board Reader" and its YAML key was `board_reader.*`.

### Calendar (broker integration)

Calendar and task management over the agent-comm broker. Disabled by default.

| JSON key                     | Type              | Default                    | Description                                  |
| ---------------------------- | ----------------- | -------------------------- | -------------------------------------------- |
| `calendar.enabled`           | `boolean`         | `false`                    | Master switch. Requires the `broker` extra.  |
| `calendar.broker_host`       | `string`          | `"ai-broker.robotsix.net"` | Broker hostname.                             |
| `calendar.broker_port`       | `integer`         | `443`                      | Broker port.                                 |
| `calendar.broker_scheme`     | `string`          | `"https"`                  | `https` (TLS) or `http`.                     |
| `calendar.broker_token`      | `string` (secret) | `""`                       | Agent bearer token registered on the broker. |
| `calendar.agent_id`          | `string`          | `"robotsix-chat"`          | This agent's id on the broker.               |
| `calendar.calendar_agent_id` | `string`          | `"robotsix-calendar"`      | Recipient calendar agent id.                 |
| `calendar.timeout`           | `number`          | `240.0`                    | Per-request timeout (seconds).               |
| `calendar.cache_ttl`         | `number`          | `60.0`                     | Cache TTL for query results (seconds).       |

### Conversation

| JSON key                          | Type      | Default                      | Description                                              |
| --------------------------------- | --------- | ---------------------------- | -------------------------------------------------------- |
| `conversation.idle_reset_seconds` | `integer` | `1800`                       | Seconds of idle before resetting the conversation store. |
| `conversation.max_history_turns`  | `integer` | `50`                         | Maximum conversation turns to retain.                    |
| `conversation.max_conversations`  | `integer` | `1000`                       | Maximum concurrent conversations.                        |
| `conversation.persist_path`       | `string`  | `"/data/conversations.json"` | Path to the conversation persistence file.               |

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

### Board Reader

Direct HTTP access to the mill's board API. Disabled by default; independent of the broker-based
mill integration.

| JSON key                    | Type              | Default                   | Description                                         |
| --------------------------- | ----------------- | ------------------------- | --------------------------------------------------- |
| `board_reader.enabled`      | `boolean`         | `false`                   | Master switch.                                      |
| `board_reader.api_base_url` | `string`          | `"http://127.0.0.1:8077"` | Base URL of the board HTTP API (no trailing slash). |
| `board_reader.api_token`    | `string` (secret) | `""`                      | Optional bearer token for the board API.            |
| `board_reader.cache_ttl`    | `number`          | `60.0`                    | Seconds to cache board API responses.               |

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

### Component Agent

Registers robotsix-chat as a discoverable component agent on the broker. Disabled by default.

| JSON key                        | Type              | Default                     | Description                                  |
| ------------------------------- | ----------------- | --------------------------- | -------------------------------------------- |
| `component_agent.enabled`       | `boolean`         | `false`                     | Master switch. Requires the `broker` extra.  |
| `component_agent.broker_host`   | `string`          | `"ai-broker.robotsix.net"`  | Broker hostname.                             |
| `component_agent.broker_port`   | `integer`         | `443`                       | Broker port.                                 |
| `component_agent.broker_scheme` | `string`          | `"https"`                   | `https` (TLS) or `http`.                     |
| `component_agent.broker_token`  | `string` (secret) | `""`                        | Agent bearer token registered on the broker. |
| `component_agent.agent_id`      | `string`          | `"robotsix-chat-component"` | This agent's responder id on the broker.     |
| `component_agent.timeout`       | `number`          | `240.0`                     | Per-request timeout (seconds).               |

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

Agent-comm broker client for sending component requests. Disabled by default.

| JSON key                      | Type            | Default | Description                                                                             |
| ----------------------------- | --------------- | ------- | --------------------------------------------------------------------------------------- |
| `component_client.enabled`    | `boolean`       | `false` | Master switch. Requires the `broker` extra.                                             |
| `component_client.timeout`    | `number`        | `240.0` | Per-request HTTP timeout (seconds).                                                     |
| `component_client.components` | `array[object]` | `[]`    | List of component targets, each with `base_url` (string) and optional `label` (string). |

### Subsessions

Background sub-agent spawning configuration.

| JSON key                               | Type      | Default                    | Description                                  |
| -------------------------------------- | --------- | -------------------------- | -------------------------------------------- |
| `subsessions.max_concurrent`           | `integer` | `8`                        | Maximum concurrent subsessions.              |
| `subsessions.max_depth`                | `integer` | `3`                        | Maximum nesting depth.                       |
| `subsessions.default_model_level`      | `integer` | `3`                        | Default model level for spawned subsessions. |
| `subsessions.min_interval_seconds`     | `number`  | `60.0`                     | Minimum interval between periodic runs.      |
| `subsessions.auto_stop_no_change_runs` | `integer` | `5`                        | Consecutive NO_CHANGE runs before auto-stop. |
| `subsessions.store_path`               | `string`  | `"/data/subsessions.json"` | Path to the subsession persistence file.     |
| `subsessions.transcript_max_entries`   | `integer` | `200`                      | Maximum transcript entries per subsession.   |

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

### Skills

Declarative skill/capability loading from YAML manifests. Disabled by default.

> **Note:** Skill manifest files (`*.skill.yaml`) are skill **content**, not runtime config. They
> remain YAML regardless of the config migration.

| JSON key               | Type      | Default           | Description                                     |
| ---------------------- | --------- | ----------------- | ----------------------------------------------- |
| `skills.enabled`       | `boolean` | `false`           | Enable manifest-driven skill loading.           |
| `skills.manifests_dir` | `string`  | `"config/skills"` | Directory containing skill YAML manifest files. |

______________________________________________________________________

## Schema

The committed [`config/config.schema.json`](../config/config.schema.json) is the authoritative
schema for the `Settings` model. It is auto-generated from the pydantic model via
`Settings.model_json_schema()` and **CI-checked** to stay in sync — a CI job regenerates it from the
model and fails the build on any drift.

To regenerate locally:

```bash
python -c 'import json; from robotsix_chat.config import Settings; print(json.dumps(Settings.model_json_schema(), indent=2))' > config/config.schema.json
```
