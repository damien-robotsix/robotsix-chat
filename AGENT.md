# robotsix-chat — agent-oriented reference

## Repo overview

robotsix-chat is a **browser + SSE chat server for an LLM agent**. It drives an LLM through
`robotsix-llmio` (pick a `model_level`, never a concrete provider) and serves it over HTTP:

- `GET /` — self-contained browser chat UI (single HTML file, no build step)
- `POST /chat` — accepts `{"message": "..."}`, returns the agent reply as SSE (`text/event-stream`)
  frames
- `GET /health` — liveness probe, returns `200 {"status": "ok"}`

Key stack: **Python ≥3.14**, **Starlette** (ASGI), `robotsix-llmio`, `pydantic`, `uvicorn`.
Entrypoint: `robotsix-chat` (console script installed by the package).

## Deploy stack structure

The repo has two compose files serving different lifecycles. Both share the same container port
(8080) and the same credential/convention patterns.

### Root `docker-compose.yml` — local dev

Builds the multi-stage `Dockerfile`, tags `robotsix-chat:local`, and runs the image with
`restart: unless-stopped`.

- **Port mapping**: `8080:8080` (host:container; container port 8080 is set by `SERVER_PORT=8080` in
  the Dockerfile).
- **Environment**: `ROBOTSIX_CONFIG_FILE=config/config.json`.
- **Volume mounts**:
  - `./config/config.json:/home/app/config/config.json:ro`
  - `~/.claude:/home/app/.claude:ro`

Prerequisites (one-time):

1. `cp config/config.example.json config/config.json`
2. `claude login` (populates `~/.claude` with subscription credentials)

### `deploy/docker-compose.yml` — production (central-deploy contract)

The production deployment is managed by
[robotsix-central-deploy](https://github.com/damien-robotsix/robotsix-central-deploy); this file is
the contract it consumes (first line: `# central-deploy-contract-version: 1`). central-deploy pulls
the pre-built `ghcr.io/damien-robotsix/robotsix-chat:main` image, injects environment secrets filled
by the operator in the dashboard, applies its own restart policy/networking/gateway routing, and
redeploys on demand — there is no Watchtower sidecar and no `restart:`/host-bind entries (both are
contract violations).

- **Service**: single `robotsix-chat` service (implicitly primary).
- **Port**: `8088:8080` — routed by the central-deploy gateway, not published on the host.
- **Environment**: carries only infrastructure wiring — the config-file path pointer
  (`ROBOTSIX_CONFIG_FILE: config/config.json`). Application config and secrets live in the mounted
  config file (injected via the `robotsix.deploy.config-target` label).
- **Volumes**: named volume `chat-data` → `/home/app/.data` (persistent agent state; flagged
  `robotsix.deploy.stateful`).
- **Claude credentials**: label `robotsix.deploy.claude-mount: "true"` — central-deploy binds the
  server's `~/.claude` to `/home/app/.claude` (the standardized container user's home) for the
  level-3 claude-sdk transport.

### Port mapping asymmetry

The Dockerfile hardcodes container port **8080** (`ENV SERVER_PORT=8080`, `EXPOSE 8080`). Neither
compose file overrides `SERVER_PORT`, so the container always listens on 8080. The host-side port
differs:

| Stack                                    | Host port             | Container port |
| ---------------------------------------- | --------------------- | -------------- |
| Local dev (`docker-compose.yml`)         | 8080                  | 8080           |
| Production (`deploy/docker-compose.yml`) | 8088 (gateway-routed) | 8080           |

## Config mount conventions

Both stacks follow the same two volume-binding patterns.

### Application config (`config/config.json`)

The app reads JSON config from `config/config.json` inside the container (resolved relative to
`WORKDIR /home/app`). The path is overridable via the `ROBOTSIX_CONFIG_FILE` env var.

- **Local dev**: mounts the single file `./config/config.json` read-only.
- **Production (central-deploy)**: configuration lives in a single JSON config file
  (`config/config.json`) mounted by central-deploy via the `robotsix.deploy.config-target`
  label. The operator copies `config/config.example.json` to `config/config.json`, fills in real
  values, and central-deploy injects it into the container. The `ROBOTSIX_CONFIG_FILE` env var (the only
  config-related key in `environment:`) points the app at this file — no application config or
  secrets live in `environment:`.

The canonical template is `config/config.example.json` (committed). The operator copies it to
`config/config.json` (gitignored).

### Claude credentials (`~/.claude`)

The `claude-sdk` transport (model level 3) authenticates via the `claude` CLI, which reads
credentials from `~/.claude` (the Claude subscription session). Local dev bind-mounts the host's
`~/.claude` to `/home/app/.claude:ro`; in production the `robotsix.deploy.claude-mount` label makes
central-deploy bind it to `/home/app/.claude` (read-write).

The operator must run `claude login` on the host before starting either stack.

### User and workdir

The Dockerfile creates a non-root user `app` (UID 1001, the standardized robotsix container layout)
with `WORKDIR /home/app`. All container-relative paths in compose files (e.g.
`config/config.json`) are relative to `/home/app`.

## Production configuration (central-deploy)

Production configuration lives in a single JSON config file (`config/config.json`) mounted by
central-deploy via the `robotsix.deploy.config-target` label. The operator fills real values (secret
slots, tuned defaults) in that file; central-deploy injects it into the container at deploy time.
The `ROBOTSIX_CONFIG_FILE` env var in the deploy compose points the app at this file — it is
infrastructure wiring only, not application configuration.

The root `.env.example` documents the application-level environment variables for **local** runs
(used by `Settings.load()` in `src/robotsix_chat/config/settings.py`):

- `ROBOTSIX_CONFIG_FILE` — config file locator (the only env var consumed for config)

## Long-term memory (cognee)

The agent is stateless by default. The optional `memory` extra adds persistent, cross-conversation
memory via embedded **cognee**: before each reply the agent `recall`s relevant memory and folds it
into the system prompt; after replying it persists the exchange (`add` + `cognify`) in a
**background task** so consolidation never adds latency. Disabled by default; a `NullMemory` no-op
is used when off or when the extra is absent — the agent then behaves exactly as before.

- **Selection**: `memory.enabled` (config). `build_memory()` returns `CogneeMemory` only when
  enabled *and* cognee is importable, else `NullMemory`.
- **Backends** (cognee runs embedded; the heavy inference is offloaded):
  - *Extraction LLM* — OpenRouter via litellm's `custom` provider
    (`openrouter/deepseek/deepseek-v4-flash`); needs `memory.llm.api_key`.
  - *Embeddings* — a remote **OpenAI-compatible** server (self-hosted Ollama / `bge-m3`, 1024-dim);
    provider must be `openai_compatible`, needs `memory.embedding.endpoint` (e.g.
    `http://host:11434/v1`). Embeddings are **not** run on the chat host.
- **Storage**: cognee's stores live under `memory.data_dir` (default `.data/cognee`) — keep it on
  the persistent `.data` bind mount so memory survives redeploys.
- **Safety**: `recall`/`remember` never raise into the chat path (errors are logged; the reply
  proceeds without memory).
- **Resilience caveat**: memory depends on the embedding server being reachable; while it's down,
  recall/consolidation silently no-op.
- **Image note**: the extra pulls a large dep tree (litellm, lancedb, transformers, …) — it is
  intentionally *not* baked into the production image until memory is enabled there (which also
  needs the embedding endpoint reachable from the host).

Config keys: `memory.enabled`, `memory.data_dir`, `memory.recall_search_type`,
`memory.llm.{provider,model,endpoint,api_key}`,
`memory.embedding.{provider,model,endpoint,dimensions,api_key,huggingface_tokenizer}`
— see `config/config.example.json` for defaults.

## Mill integration (agent-comm broker)

When `mill.enabled` is set, the chat agent gains a **tool** (`consult_mill`) that the LLM calls to
forward a natural-language request to the robotsix-mill **board manager**
(`board-manager-robotsix-mill`) over the shared **agent-comm broker**, and relays its reply — so a
user can have the mill track/do development work (create/triage tickets, ask status) from chat.
Disabled by default; no tools are added when off or when the `broker` extra is absent.

- **Pattern**: mirrors `robotsix-cost-monitor`'s cost-analyst → board flow — a per-call pull/mailbox
  `robotsix_agent_comm.sdk.agent.Agent` + `create_transport_pair("brokered", …)`, sending
  `{"message": <NL>}` (plus optional `repo_id`) to the board manager. The blocking call is offloaded
  with `asyncio.to_thread`; failures degrade to a message the LLM relays (never raise).
- **Tool wiring**: `build_mill_tools(settings.mill)` returns the async `consult_mill` callable;
  `create_agent_from_settings` passes it as `LlmioChatAgent(tools=…)` →
  `provider.build_agent(tools=…)`. The claude-sdk transport runs a real tool loop (in-SDK MCP); the
  final reply is still one block.
- **Broker auth**: the agent authenticates with a bearer token (`mill.broker_token`) registered on
  the broker under its `agent_id` (`robotsix-chat`). The board manager decides the target repo
  unless `repo_id` is set. Broker is public TLS at `ai-broker.robotsix.net:443` (no custom CA).
- **Config keys**:
  `mill.{enabled,broker_host,broker_port,broker_scheme,broker_token,agent_id,board_manager_id,repo_id,timeout}`
  — see `config/config.example.json` for defaults.

## Submodule layout

- **`broker_src/`** — vendors the `robotsix-agent-comm` repository (the agent-comm broker's source
  code). Changes to the broker itself — new routes, protocol changes, the monitoring UI — must be
  developed in the `damien-robotsix/robotsix-agent-comm` repo and pinned here as a submodule commit
  update. Do not develop broker features directly inside `broker_src/` within this repo.

## Key file map

- `docker-compose.yml` — local dev compose (builds from Dockerfile, tag `robotsix-chat:local`)
- `deploy/docker-compose.yml` — production stack (GHCR image + Watchtower, loopback-only)
- `Dockerfile` — multi-stage build (`python:3.14-slim`, Node.js + `claude` CLI, non-root `app`,
  `EXPOSE 8080`)
- `.env.example` — canonical env-var reference (standard variables; deploy-only vars documented
  above)
- `config/config.example.json` — canonical JSON config template (copy to `config.json`)
- `src/robotsix_chat/config/settings.py` — settings cascade (pydantic defaults → YAML → env,
  `Settings.load()`); includes `MemorySettings`
- `src/robotsix_chat/memory/` — optional long-term memory: `base.py` (`ChatMemory` protocol +
  `NullMemory`), `cognee.py` (`CogneeMemory`), `__init__.py` (`build_memory()`)
- `src/robotsix_chat/mill/` — optional mill-via-broker tool: `client.py` (`MillClient` —
  cost-analyst pattern), `__init__.py` (`build_mill_tools()`)
- `src/robotsix_chat/chat/server.py` — Starlette ASGI app; `GET /`, `POST /chat`, `GET /health`
- `.github/workflows/release-image.yml` — GHCR publish workflow (triggers on `main` push, `v*` tag,
  manual dispatch)

## CI workflow conventions

**Rule:** All third-party GitHub Actions must be pinned by immutable 40-character commit SHA with
the semantic version as a trailing comment (e.g.
`actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2`). Do not use mutable tags
(`@v4`, `@v3`) without a SHA. Dependabot's `package-ecosystem: github-actions` will auto-update SHAs
on new releases.

**Rule:** All reusable workflow references (distinguishable by the `.github/workflows/` path
component in the `uses:` value) must use the full 40-character commit SHA of the target repo's
current HEAD on its default branch. Never use mutable refs (`@main`, `@master`, `@v1`, `@latest`).
Add a trailing version comment for readability (e.g. `# v0.2.0` or `# main`).

## Testing conventions

Tests for module `robotsix_chat.<module>` live under `tests/<module>/`, mirroring the per-module
source layout (e.g. `tests/chat/` for `robotsix_chat.chat`, `tests/config/` for
`robotsix_chat.config`). Do not place tests directly in the `tests/` root.

**Rule:** When testing a module that lazy-imports `robotsix_agent_comm`, both monkeypatch
`importlib.util.find_spec` AND populate `sys.modules` with a fake module stub. Use the
`_install_fake_agent_comm(monkeypatch)` helper from `tests/conftest.py` rather than only patching
`find_spec` — the lazy `from robotsix_agent_comm.sdk import BrokeredRequester` import resolves at
class-construction time through `sys.modules`, not through `find_spec`.

**Rule:** When a `ChatAgent` protocol parameter is added or changed, update ALL mock classes that
implement the protocol (`_MockAgent`, `MockAgent`, and any other test-local mocks) in the same PR.
Run `mypy` on the full test suite to verify protocol conformance — a mock that lacks a keyword
argument silently passes structural subtyping at runtime but fails static `mypy --strict` checks.

**Rule:** When adding a new env-var override in a `_build_*_raw()` function, add both a
`_wipe_env_vars` entry AND a test that sets the env var (via `monkeypatch.setenv`) and asserts the
resulting field value. Follow the `BOARD_READER_CACHE_TTL` / `test_board_reader_from_env` sibling
pattern.

## Task tracking

Persistent, human-readable task tracking lives under `tasks/` at the repo root:

- `tasks/TASKS.md` — active tasks (pending, in-progress, blocked).
- `tasks/ARCHIVE.md` — completed tasks (history preserved).
- `tasks/README.md` — documents the format and the read/add/update/archive workflow.

At the start of every conversation, read `tasks/TASKS.md` to pick up any pending work from prior
conversations. When work is done, archive the task by moving its section from `TASKS.md` into
`ARCHIVE.md`. The format is structured Markdown — a person can inspect or edit the files by hand.

This repo follows the
[robotsix stack standards](https://github.com/damien-robotsix/robotsix-standards).
