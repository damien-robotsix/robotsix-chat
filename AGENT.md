# robotsix-chat — agent-oriented reference

This repo follows the
[robotsix stack standards](https://github.com/damien-robotsix/robotsix-standards); read those first
— this file carries only repo-specific knowledge.

## Repo overview

robotsix-chat is a **deployable component** (per the standards' distribution tiers): a browser + SSE
chat server for an LLM agent. It drives an LLM through `robotsix-llmio` (pick a `model_level`, never
a concrete provider) and serves it over HTTP:

- `GET /` — self-contained browser chat UI (single HTML file, no build step)
- `POST /chat` — accepts `{"message": "..."}`, returns the agent reply as SSE (`text/event-stream`)
  frames
- `GET /health` — liveness probe, returns `200 {"status": "ok"}`

Key stack: **Python ≥3.14**, **Starlette** (ASGI), `robotsix-llmio`, `pydantic`, `uvicorn`.
Entrypoint: `robotsix-chat` (console script installed by the package).

## Configuration (config standard)

One JSON file, loaded by `robotsix_config.load_config` into the pydantic `Settings` model
(`src/robotsix_chat/config/settings.py`). No env overlay, no CLI merge — the file is the only source
of values; model field defaults fill the gaps.

- `config/config.json` — **committed defaults template** (what central-deploy merges operator edits
  into). Never put real credentials in it.
- `config/config.schema.json` — committed typed JSON Schema, generated from `Settings`; the CI
  `check-config-schema` job fails when it drifts from the model.
- `ROBOTSIX_CONFIG_FILE` — the one env var, and it only *locates* the file. For local runs with real
  credentials, copy the template to the gitignored `config/config.local.json` and point
  `ROBOTSIX_CONFIG_FILE` at it.
- The server binds `server_host:server_port` from the config file (template default
  `127.0.0.1:8000`; containers need `0.0.0.0:8080` in their mounted config).

## Deploy stack structure

Two compose files with different jobs (component standard):

### Root `docker-compose.yml` — local dev

Builds the multi-stage `Dockerfile`, tags `robotsix-chat:local`. Mounts `./config/config.local.json`
read-only at `/home/app/config/config.json` and the host `~/.claude` at `/home/app/.claude` (run
`claude login` once beforehand).

### `deploy/docker-compose.yml` — production (central-deploy contract)

Consumed by [robotsix-central-deploy](https://github.com/damien-robotsix/robotsix-central-deploy)
(first line: `# central-deploy-contract-version: 1`); central-deploy pulls
`ghcr.io/damien-robotsix/robotsix-chat:main`, applies its own lifecycle (restart, networking,
gateway routing), and redeploys on operator demand — no Watchtower, no `restart:`, no host binds.

- **Service**: single `robotsix-chat` service (implicitly primary).
- **Port**: `8088:8080` — the primary port is gateway-routed (`deploy.robotsix.net/<component>/*`).
- **Config**: label `robotsix.deploy.config-target: "/home/app/config/config.json"` + the
  `chat-config` volume mounted at `/home/app/config`; central-deploy writes the merged config there
  before every start. `ROBOTSIX_CONFIG_FILE` in `environment:` is wiring only.
- **State**: named volume `chat-data` → `/data` (knowledge store, cognee memory, HF cache); starts
  empty on first onboard.
- **Claude credentials**: label `robotsix.deploy.claude-mount: "true"` — central-deploy mounts its
  managed `claude-auth` named volume at `/home/app/.claude` (levels 3-4 claude-sdk transport).
  Authenticate via central-deploy's dashboard login flow, never by preparing host files.

### Container layout

Standardized robotsix layout (docker standard): non-root user `app`, uid/gid **1000**,
`WORKDIR /home/app`; the container listens on **8080** (from the mounted config), `EXPOSE 8080`,
stdlib-only `HEALTHCHECK` on `/health`, exec-form `ENTRYPOINT ["robotsix-chat"]` (no entrypoint.sh).

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
- **Storage**: cognee's stores live under `memory.data_dir` (default `/data/cognee`) — keep it on
  the persistent `/data` volume so memory survives redeploys.
- **Tracing**: cognee traffic uses its **own** Langfuse project (`robotsix-chat-cognee`) with
  dedicated `memory.langfuse.*` credential fields — never the main `langfuse.*` credentials
  (component standard: one Langfuse project per LLM-generating function).
- **Safety**: `recall`/`remember` never raise into the chat path (errors are logged; the reply
  proceeds without memory).
- **Resilience caveat**: memory depends on the embedding server being reachable; while it's down,
  recall/consolidation silently no-op.

Config keys: `memory.enabled`, `memory.data_dir`, `memory.recall_search_type`,
`memory.llm.{provider,model,endpoint,api_key}`,
`memory.embedding.{provider,model,endpoint,dimensions,api_key,huggingface_tokenizer}` — see
`config/config.json` for defaults.

## Key file map

- `docker-compose.yml` — local dev compose (builds from Dockerfile, tag `robotsix-chat:local`)
- `deploy/docker-compose.yml` — production deploy contract (central-deploy; GHCR image)
- `Dockerfile` — multi-stage build (`python:3.14-slim`, Node.js + `claude` CLI, non-root `app`,
  `EXPOSE 8080`)
- `config/config.json` — committed JSON config defaults template
- `config/config.schema.json` — committed typed schema (CI-checked against `Settings`)
- `src/robotsix_chat/config/settings.py` — `Settings` (pydantic) + `robotsix_config.load_config`
- `src/robotsix_chat/memory/` — optional long-term memory: `base.py` (`ChatMemory` protocol +
  `NullMemory`), `cognee.py` (`CogneeMemory`), `__init__.py` (`build_memory()`)
- `src/robotsix_chat/chat/server/` — Starlette ASGI app (`app.py`, `routes.py`, `cli.py`); `GET /`,
  `POST /chat`, `GET /health`
- `.github/workflows/release-image.yml` — GHCR publish caller (shared `docker-release.yml`)

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

**Rule:** The `image-scan` job in `ci.yml` is deliberately hand-rolled (not the shared
`docker-pr-scan.yml`): the shared workflow uses the GHA layer cache, which was measured at 45-55 min
per run on this multi-GB image vs ~4 min cold. Do not switch back without timing both paths (docker
standard, "CI-time image scan").

**Rule:** The dependency CVE audit runs in the `lockfile` job (with the curated
`--ignore-until-fixed` list), and `run-audit: false` is passed to the shared `python-ci.yml`. The
shared audit step has no ignore mechanism, so enabling both hard-blocks CI on CVEs that have no
released fix.

## Testing conventions

Tests for module `robotsix_chat.<module>` live under `tests/<module>/`, mirroring the per-module
source layout (e.g. `tests/chat/` for `robotsix_chat.chat`, `tests/config/` for
`robotsix_chat.config`). Do not place tests directly in the `tests/` root.

**Rule:** When a `ChatAgent` protocol parameter is added or changed, update ALL mock classes that
implement the protocol (`_MockAgent`, `MockAgent`, and any other test-local mocks) in the same PR.
Run `mypy` on the full test suite to verify protocol conformance — a mock that lacks a keyword
argument silently passes structural subtyping at runtime but fails static `mypy --strict` checks.

## Task tracking

Persistent, human-readable task tracking lives under `tasks/` at the repo root:

- `tasks/TASKS.md` — active tasks (pending, in-progress, blocked).
- `tasks/ARCHIVE.md` — completed tasks (history preserved).
- `tasks/README.md` — documents the format and the read/add/update/archive workflow.

At the start of every conversation, read `tasks/TASKS.md` to pick up any pending work from prior
conversations. When work is done, archive the task by moving its section from `TASKS.md` into
`ARCHIVE.md`. The format is structured Markdown — a person can inspect or edit the files by hand.
