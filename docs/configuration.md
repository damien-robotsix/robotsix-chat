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

- `openrouter_api_key` (accepts the legacy key `llmio_api_key` as an alias)
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

| JSON key                    | Type            | Default                                               | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| --------------------------- | --------------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent_instruction`         | `string`        | (long default)                                        | System instruction for the agent. Governed by the code default in `src/robotsix_chat/config/settings.py` (currently v163). Intentionally absent from `config/config.json` — the code default is the single source of truth. Operators who need to override it can add `"agent_instruction"` to their local or deployed config file; doing so bypasses the code default entirely. The agent's reply style is governed separately by [`docs/prompt-style.md`](prompt-style.md) — that file is automatically injected into every system prompt build and is the single source of truth for reply formatting. |
| `max_images_per_message`    | `integer`       | `8`                                                   | Maximum images per chat message.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `max_image_bytes`           | `integer`       | `5242880`                                             | Maximum image size in bytes (5 MiB).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `allowed_image_media_types` | `array[string]` | `["image/png","image/jpeg","image/gif","image/webp"]` | Allowed image MIME types.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

### LLM I/O

Model selection, capability levels, API keys, and token budgeting for the chat and auxiliary LLM
pipelines (summary, vision captioning).

| JSON key                        | Type              | Default                         | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------- | ----------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `chat_default_model_level`      | `integer`         | `2`                             | The chat agent's default capability tier: `1` (cheap/frequent), `2` (workhorse, default), `3` (frontier). Levels are a pure capability axis; the serving provider is llmio's failover axis (keyless Claude SDK default slot, keyed OpenRouter fallback slot).                                                                                                                                                                                                                                                                                      |
| `summary_model_level`           | `integer`         | `1`                             | Capability level of the dedicated summariser agent (idle-timeout compaction summary, carryover summary, conversation titles). Runs once per idle gap, not per turn; a bounded text transformation, so the cheap/frequent level.                                                                                                                                                                                                                                                                  |
| `openrouter_api_key`            | `string` (secret) | `""`                            | OpenRouter API key for llmio's keyed fallback slot (and vision captioning). Required for levels 1–2; ignored for 3–4. Accepts the legacy key `llmio_api_key` as a backward-compatible alias. Distinct from the top-level `openrouter` credential block (memory subsystem).                                                                                                                                                                                                                                       |
| `llmio_failover_window_seconds` | `number`          | `900.0`                         | How long llmio routes calls straight to the fallback (OpenRouter) provider slot after the default (Claude) slot fails repeatedly or exhausts its quota, before automatically returning to the default.                                                                                                                                                                                                                                                                                                                                                                                           |
| `llmio_tier_overrides`          | `object`          | `{}`                            | Overrides merged over llmio's baked tier config, in load_tier_config's nested shape — e.g. `{"fallback": {"level2": {"model": "openrouter-<model>"}}}` to change which model serves a capability level on a provider slot. Per-level dicts merge field-by-field over the baked binding; unknown keys are rejected. The failover window from `llmio_failover_window_seconds` is layered on top. Edited in the settings panel as a validated JSON textarea; the saved value must be a JSON object. |
| `vision_model`                  | `string`          | `openrouter/openai/gpt-4o-mini` | OpenRouter model id for automatic image captioning. When configured (non-empty), tools like `render_pdf_page` and `render_url` will send images to this model for a one-shot text caption when the active chat model lacks vision support, so text-only models can still understand image content. Empty string disables captioning; images are then omitted with a curated note on text-only models.                                                                                            |

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

**Context reduction — one mechanism.** Idle-timeout compaction was removed. The summary-compaction
scheduler (see the Summary compaction section) is the single way ANY session's context shrinks:
every `evergoing.trim_interval_seconds` it inspects each session with new input and, when more than
`evergoing.keep_recent_runs` fresh runs accumulated beyond the previous summary, folds the older
runs into the session summary. Nothing is dropped from the UI transcript.

### Langfuse (tracing)

The canonical component-standard credential block: the instance host plus every Langfuse project
this component traces to, keyed by **project name**.

| JSON key                                 | Type              | Default                        | Description                           |
| ---------------------------------------- | ----------------- | ------------------------------ | ------------------------------------- |
| `langfuse.host`                          | `string`          | `"https://cloud.langfuse.com"` | Langfuse instance base URL.           |
| `langfuse.projects.<project>.public_key` | `string` (secret) | `""`                           | Langfuse public key for that project. |
| `langfuse.projects.<project>.secret_key` | `string` (secret) | `""`                           | Langfuse secret key for that project. |
| `langfuse.projects.<project>.project_id` | `string`          | `""`                           | Optional Langfuse project id.         |

This component declares two projects per the component standard's one-project-per-LLM-function rule:

- `robotsix-chat` — the main chat agent.
- `robotsix-chat-cognee` — the cognee/LiteLLM memory pipeline, named by `memory.langfuse_project`.
  See [Memory](#memory-cognee).

**Additional projects for cross-component trace analysis:** You can also add credentials for other fleet components' Langfuse projects (e.g. `mill`, `invest`, `ci_fix`) to enable the `inspect_langfuse_trace` tool to analyze their traces. This is useful for periodic cost-review tasks that need to identify cost drivers across all high-spend projects.

**Example configuration with multi-project trace inspection:**

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
    },
    "mill": {
      "public_key": "pk-lf-...",
      "secret_key": "sk-lf-...",  // pragma: allowlist secret
      "project_id": ""
    },
    "invest": {
      "public_key": "pk-lf-...",
      "secret_key": "sk-lf-...",  // pragma: allowlist secret
      "project_id": ""
    }
  }
}
```

With these credentials configured, the `inspect_langfuse_trace` tool can query traces from any of these projects by passing the `project` parameter — e.g. `project="mill"` to inspect mill traces, or `project="invest"` to inspect invest traces. See the [Langfuse inspect skill](../src/robotsix_chat/langfuse/skill.md) for examples.

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
| ---------------------------------------------- | ----------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
