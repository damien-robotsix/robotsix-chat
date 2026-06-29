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
- **Environment**: `LLMIO_MODEL_LEVEL=3`, `CHAT_CONFIG_PATH=config/chat.local.yaml`.
- **Volume mounts**:
  - `./config/chat.local.yaml:/home/appuser/config/chat.local.yaml:ro`
  - `~/.claude:/home/appuser/.claude:ro`

Prerequisites (one-time):

1. `cp config/chat.local.example.yaml config/chat.local.yaml`
2. `claude login` (populates `~/.claude` with subscription credentials)

### `deploy/docker-compose.yml` — production

Pulls `ghcr.io/damien-robotsix/robotsix-chat:${IMAGE_TAG}` (no build), binds loopback-only, and adds
a Watchtower sidecar for auto-redeploy.

- **Services**: `chat` + `watchtower`, both `restart: unless-stopped`.
- **Port mapping**: `127.0.0.1:${CHAT_PORT:-8088}:8080` (loopback only; `CHAT_PORT` defaults to
  8088).
- **Environment** (chat service):
  - `CHAT_CONFIG_PATH=config/chat.local.yaml`
  - `LLMIO_MODEL_LEVEL=3`
  - `AUTH_ENABLED=1` (hardcoded; ignores any `.env` value)
  - `AUTH_PASSWORD=${CHAT_AUTH_PASSWORD:?err}` (mandatory; compose refuses to start if unset)
- **Volume mounts** (chat service):
  - `./config:/home/appuser/config` (whole directory, read-write — config changes persist across
    redeploys)
  - `./data:/home/appuser/.data` (persistent agent data, e.g. memory — read-write; survives
    container redeploys. Follows the fleet convention shared with
    `robotsix-cost-monitor`/`robotsix-auto-mail`, which bind-mount `./data` into the container's
    `.data` dir.)
  - `~/.claude:/home/appuser/.claude:ro`
- **Watchtower** sidecar: `containrrr/watchtower`, `--interval 30 --label-enable --cleanup`,
  targeting the label `com.centurylinklabs.watchtower.enable=true` on the chat container.

### Port mapping asymmetry

The Dockerfile hardcodes container port **8080** (`ENV SERVER_PORT=8080`, `EXPOSE 8080`). Neither
compose file overrides `SERVER_PORT`, so the container always listens on 8080. The host-side port
differs:

| Stack                                    | Host port            | Container port |
| ---------------------------------------- | -------------------- | -------------- |
| Local dev (`docker-compose.yml`)         | 8080                 | 8080           |
| Production (`deploy/docker-compose.yml`) | `${CHAT_PORT:-8088}` | 8080           |

## Config mount conventions

Both stacks follow the same two volume-binding patterns.

### Application config (`config/chat.local.yaml`)

The app reads YAML config from `config/chat.local.yaml` inside the container (resolved relative to
`WORKDIR /home/appuser`). The path is overridable via the `CHAT_CONFIG_PATH` env var.

- **Local dev**: mounts the single file `./config/chat.local.yaml` read-only.
- **Production**: mounts the whole `./config/` directory read-write (so config edits survive
  container redeploys).

The canonical template is `config/chat.local.example.yaml` (committed). The operator copies it to
`config/chat.local.yaml` (gitignored). For the production stack the `./config/` directory must exist
**under `deploy/`** (i.e. `deploy/config/chat.local.yaml`) because the compose-file-relative path
`./config` resolves to `deploy/config/`.

### Claude credentials (`~/.claude`)

The `claude-sdk` transport (model level 3) authenticates via the `claude` CLI, which reads
credentials from `~/.claude` (the Claude subscription session). Both stacks bind-mount the host's
`~/.claude` to `/home/appuser/.claude:ro`.

The operator must run `claude login` on the host before starting either stack.

### User and workdir

The Dockerfile creates a non-root user `appuser` (UID 10001) with `WORKDIR /home/appuser`. All
container-relative paths in compose files (e.g. `config/chat.local.yaml`) are relative to
`/home/appuser`.

## .env for production deploy

The deploy stack uses **two** `.env` files for different purposes:

### `deploy/.env` — docker-compose variable substitution

`docker compose -f deploy/docker-compose.yml up` reads `.env` from the **project directory**
(`deploy/`). Copy the template:

```
cp deploy/.env.example deploy/.env
```

Then set the three variables it defines:

| Variable             | Required | Default | Purpose                                                                                    |
| -------------------- | -------- | ------- | ------------------------------------------------------------------------------------------ |
| `IMAGE_TAG`          | yes      | (none)  | GHCR image tag — `main` (Watchtower's continuous-deploy target), `v1.2.3`, or a commit SHA |
| `CHAT_PORT`          | no       | `8088`  | Loopback host port the chat service binds                                                  |
| `CHAT_AUTH_PASSWORD` | **yes**  | (none)  | HTTP Basic Auth password; passed through as `AUTH_PASSWORD` inside the container           |

The compose file uses `${CHAT_AUTH_PASSWORD:?err}` — `docker compose up` refuses to start if this is
missing or empty.

### Root `.env` — application runtime env vars

The root `.env.example` documents the full set of application-level environment variables that
`python-dotenv` loads at runtime (used by `Settings.load()` in `src/robotsix_chat/config.py`):

- `LLMIO_MODEL_LEVEL`, `LLMIO_API_KEY` — LLM selection
- `SERVER_HOST`, `SERVER_PORT`, `LOG_LEVEL`, `CORS_ALLOW_ORIGINS` — server
- `AUTH_ENABLED`, `AUTH_USERNAME`, `AUTH_PASSWORD` — HTTP Basic Auth

In the **deploy stack** most of these are set explicitly in the compose `environment:` block and
override any `.env` values. Notably, `AUTH_ENABLED` is hardcoded to `1` — the production stack
always requires HTTP Basic Auth regardless of what `.env` or the YAML config says.

### Config file under deploy/

The production stack mounts `./config` from the compose-file directory (i.e. `deploy/config/`).
Create it from the canonical template:

```
mkdir -p deploy/config
cp config/chat.local.example.yaml deploy/config/chat.local.yaml
```

Edit `deploy/config/chat.local.yaml` as needed.

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
`memory.embedding.{provider,model,endpoint,dimensions,api_key,huggingface_tokenizer}` — each with a
`MEMORY_*` env override (see `.env.example`).

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
  — each with a `MILL_*` env override.

## Submodule layout

- **`broker_src/`** — vendors the `robotsix-agent-comm` repository (the agent-comm broker's source
  code). Changes to the broker itself — new routes, protocol changes, the monitoring UI — must be
  developed in the `damien-robotsix/robotsix-agent-comm` repo and pinned here as a submodule commit
  update. Do not develop broker features directly inside `broker_src/` within this repo.

## Key file map

- `docker-compose.yml` — local dev compose (builds from Dockerfile, tag `robotsix-chat:local`)
- `deploy/docker-compose.yml` — production stack (GHCR image + Watchtower, loopback-only)
- `Dockerfile` — multi-stage build (`python:3.14-slim`, Node.js + `claude` CLI, non-root `appuser`,
  `EXPOSE 8080`)
- `.env.example` — canonical env-var reference (standard variables; deploy-only vars documented
  above)
- `config/chat.local.example.yaml` — canonical YAML config template (copy to `chat.local.yaml`)
- `src/robotsix_chat/config.py` — settings cascade (pydantic defaults → YAML → env,
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
