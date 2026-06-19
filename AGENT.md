# robotsix-chat — agent-oriented reference

## Repo overview

robotsix-chat is a **browser + SSE chat server for an LLM agent**.
It drives an LLM through `robotsix-llmio` (pick a `model_level`, never a concrete provider) and serves it over HTTP:

- `GET /` — self-contained browser chat UI (single HTML file, no build step)
- `POST /chat` — accepts `{"message": "..."}`, returns the agent reply as SSE (`text/event-stream`) frames
- `GET /health` — liveness probe, returns `200 {"status": "ok"}`

Key stack: **Python ≥3.12**, **Starlette** (ASGI), `robotsix-llmio`, `pydantic`, `uvicorn`.
Entrypoint: `robotsix-chat` (console script installed by the package).

## Deploy stack structure

The repo has two compose files serving different lifecycles.  Both share the same container port (8080) and the same credential/convention patterns.

### Root `docker-compose.yml` — local dev

Builds the multi-stage `Dockerfile`, tags `robotsix-chat:local`, and runs the image with `restart: unless-stopped`.

- **Port mapping**: `8080:8080` (host:container; container port 8080 is set by `SERVER_PORT=8080` in the Dockerfile).
- **Environment**: `LLMIO_MODEL_LEVEL=3`, `CHAT_CONFIG_PATH=config/chat.local.yaml`.
- **Volume mounts**:
  - `./config/chat.local.yaml:/home/appuser/config/chat.local.yaml:ro`
  - `~/.claude:/home/appuser/.claude:ro`

Prerequisites (one-time):
1. `cp config/chat.local.example.yaml config/chat.local.yaml`
2. `claude login` (populates `~/.claude` with subscription credentials)

### `deploy/docker-compose.yml` — production

Pulls `ghcr.io/damien-robotsix/robotsix-chat:${IMAGE_TAG}` (no build), binds loopback-only, and adds a Watchtower sidecar for auto-redeploy.

- **Services**: `chat` + `watchtower`, both `restart: unless-stopped`.
- **Port mapping**: `127.0.0.1:${CHAT_PORT:-8088}:8080` (loopback only; `CHAT_PORT` defaults to 8088).
- **Environment** (chat service):
  - `CHAT_CONFIG_PATH=config/chat.local.yaml`
  - `LLMIO_MODEL_LEVEL=3`
  - `AUTH_ENABLED=1` (hardcoded; ignores any `.env` value)
  - `AUTH_PASSWORD=${CHAT_AUTH_PASSWORD:?err}` (mandatory; compose refuses to start if unset)
- **Volume mounts** (chat service):
  - `./config:/home/appuser/config` (whole directory, read-write — config changes persist across redeploys)
  - `~/.claude:/home/appuser/.claude:ro`
- **Watchtower** sidecar: `containrrr/watchtower`, `--interval 30 --label-enable --cleanup`, targeting the label `com.centurylinklabs.watchtower.enable=true` on the chat container.

### Port mapping asymmetry

The Dockerfile hardcodes container port **8080** (`ENV SERVER_PORT=8080`, `EXPOSE 8080`).  Neither compose file overrides `SERVER_PORT`, so the container always listens on 8080.  The host-side port differs:

| Stack | Host port | Container port |
|-------|-----------|----------------|
| Local dev (`docker-compose.yml`) | 8080 | 8080 |
| Production (`deploy/docker-compose.yml`) | `${CHAT_PORT:-8088}` | 8080 |

## Config mount conventions

Both stacks follow the same two volume-binding patterns.

### Application config (`config/chat.local.yaml`)

The app reads YAML config from `config/chat.local.yaml` inside the container (resolved relative to `WORKDIR /home/appuser`).  The path is overridable via the `CHAT_CONFIG_PATH` env var.

- **Local dev**: mounts the single file `./config/chat.local.yaml` read-only.
- **Production**: mounts the whole `./config/` directory read-write (so config edits survive container redeploys).

The canonical template is `config/chat.local.example.yaml` (committed).  The operator copies it to `config/chat.local.yaml` (gitignored).  For the production stack the `./config/` directory must exist **under `deploy/`** (i.e. `deploy/config/chat.local.yaml`) because the compose-file-relative path `./config` resolves to `deploy/config/`.

### Claude credentials (`~/.claude`)

The `claude-sdk` transport (model level 3) authenticates via the `claude` CLI, which reads credentials from `~/.claude` (the Claude subscription session).  Both stacks bind-mount the host's `~/.claude` to `/home/appuser/.claude:ro`.

The operator must run `claude login` on the host before starting either stack.

### User and workdir

The Dockerfile creates a non-root user `appuser` (UID 10001) with `WORKDIR /home/appuser`.  All container-relative paths in compose files (e.g. `config/chat.local.yaml`) are relative to `/home/appuser`.

## .env for production deploy

The deploy stack uses **two** `.env` files for different purposes:

### `deploy/.env` — docker-compose variable substitution

`docker compose -f deploy/docker-compose.yml up` reads `.env` from the
**project directory** (`deploy/`).  Copy the template:

```
cp deploy/.env.example deploy/.env
```

Then set the three variables it defines:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `IMAGE_TAG` | yes | (none) | GHCR image tag — `main` (Watchtower's continuous-deploy target), `v1.2.3`, or a commit SHA |
| `CHAT_PORT` | no | `8088` | Loopback host port the chat service binds |
| `CHAT_AUTH_PASSWORD` | **yes** | (none) | HTTP Basic Auth password; passed through as `AUTH_PASSWORD` inside the container |

The compose file uses `${CHAT_AUTH_PASSWORD:?err}` — `docker compose up`
refuses to start if this is missing or empty.

### Root `.env` — application runtime env vars

The root `.env.example` documents the full set of application-level
environment variables that `python-dotenv` loads at runtime (used by
`Settings.load()` in `src/robotsix_chat/config.py`):

- `LLMIO_MODEL_LEVEL`, `LLMIO_API_KEY` — LLM selection
- `SERVER_HOST`, `SERVER_PORT`, `LOG_LEVEL`, `CORS_ALLOW_ORIGINS` — server
- `AUTH_ENABLED`, `AUTH_USERNAME`, `AUTH_PASSWORD` — HTTP Basic Auth

In the **deploy stack** most of these are set explicitly in the compose
`environment:` block and override any `.env` values.  Notably,
`AUTH_ENABLED` is hardcoded to `1` — the production stack always requires
HTTP Basic Auth regardless of what `.env` or the YAML config says.

### Config file under deploy/

The production stack mounts `./config` from the compose-file directory
(i.e. `deploy/config/`).  Create it from the canonical template:

```
mkdir -p deploy/config
cp config/chat.local.example.yaml deploy/config/chat.local.yaml
```

Edit `deploy/config/chat.local.yaml` as needed.

## Key file map

- `docker-compose.yml` — local dev compose (builds from Dockerfile, tag `robotsix-chat:local`)
- `deploy/docker-compose.yml` — production stack (GHCR image + Watchtower, loopback-only)
- `Dockerfile` — multi-stage build (`python:3.13-slim-bookworm`, Node.js + `claude` CLI, non-root `appuser`, `EXPOSE 8080`)
- `.env.example` — canonical env-var reference (standard variables; deploy-only vars documented above)
- `config/chat.local.example.yaml` — canonical YAML config template (copy to `chat.local.yaml`)
- `src/robotsix_chat/config.py` — settings cascade (pydantic defaults → YAML → env, `Settings.load()`)
- `src/robotsix_chat/chat/server.py` — Starlette ASGI app; `GET /`, `POST /chat`, `GET /health`
- `.github/workflows/release-image.yml` — GHCR publish workflow (triggers on `main` push, `v*` tag, manual dispatch)
