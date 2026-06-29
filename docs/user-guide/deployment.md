# Deployment

End-to-end guide: build locally, publish to GHCR, deploy the pull-based Docker Compose stack, and
reach the loopback service.

______________________________________________________________________

## Overview

robotsix-chat ships as a single Docker image published to `ghcr.io/damien-robotsix/robotsix-chat`.
There are two Compose files:

| File                             | Purpose                                                  |
| -------------------------------- | -------------------------------------------------------- |
| `docker-compose.yml` (repo root) | Local build-and-run loop for development                 |
| `deploy/docker-compose.yml`      | Pull-based production stack with Watchtower auto-updates |

The production stack binds to **loopback only** (`127.0.0.1`). A reverse proxy (nginx, Caddy, …) is
expected in front of it — see the [reverse proxy placeholder](#reverse-proxy--tls) at the end of
this guide.

______________________________________________________________________

## 1. Local build and run

The repo-root `docker-compose.yml` builds the image from source and starts a single `chat`
container. Use this for development, testing, or ad-hoc runs.

### Prerequisites

- Docker Engine 24+ and Compose v2
- Claude subscription: `claude login` (populates `~/.claude`)
- **Persistent `.data` volume**: conversation history is written to `.data/conversations.json`
  inside the container. The deploy stack mounts `./data` at `/home/appuser/.data` (read-write) so
  chat history survives container restarts. Ensure the `deploy/data/` directory exists (or let
  Docker create it).

### Steps

```bash
# 1. Create a local config file
cp config/chat.local.example.yaml config/chat.local.yaml

# 2. (Optional) Edit config/chat.local.yaml to set auth.enabled: true
#    and auth.password if you want Basic auth on the dev instance.

# 3. Build and start
docker compose up --build

# The chat server is now reachable at http://127.0.0.1:8080
```

The compose file mounts `config/chat.local.yaml` and `~/.claude` read-only into the container. No
`.env` file is needed — the compose file sets `LLMIO_MODEL_LEVEL=3` (Claude SDK / Opus, keyless) and
`CHAT_CONFIG_PATH=config/chat.local.yaml` directly.

To stop: `docker compose down`.

______________________________________________________________________

## 2. Publishing to GHCR

The [release-image workflow](/.github/workflows/release-image.yml) builds and pushes the image to
`ghcr.io/damien-robotsix/robotsix-chat`. It triggers on:

| Trigger                       | Tags pushed                        |
| ----------------------------- | ---------------------------------- |
| Push to `main`                | `main`, `sha-<short>`              |
| Push of a `v*` tag (`v1.2.3`) | `1.2.3`, `1.2`, `1`, `sha-<short>` |
| Manual (`workflow_dispatch`)  | same as branch/tag                 |

Every build also produces SLSA provenance and a CycloneDX/SPDX SBOM attestation.

### How to publish

**Continuous deploy (moving `main` tag):**

```bash
# Merge to main (or push directly if permitted).  The workflow runs
# automatically and updates the `main` tag on GHCR.  Watchtower on the
# deploy host picks it up within 30 seconds.
```

**Semver release:**

```bash
git tag v1.2.3
git push origin v1.2.3
# The workflow pushes tags: 1.2.3, 1.2, 1, sha-<short>
```

There is no CI-to-deploy automation — the release workflow only publishes images. The deploy host
pulls them independently.

______________________________________________________________________

## 3. Deploying the production stack

The `deploy/` stack pulls a pre-built image from GHCR and runs it with Watchtower for automatic
updates.

### One-time host setup

```bash
# 1. Claude credentials (see Claude credentials section below)
claude login

# 2. Create the config directory and file
mkdir -p deploy/config
cp config/chat.local.example.yaml deploy/config/chat.local.yaml
# Edit deploy/config/chat.local.yaml:
#   - Set auth.enabled: true
#   - Optionally set auth.username (default: admin)
#   - The auth.password field can be left empty — the compose file
#     supplies it via the AUTH_PASSWORD env var

# 3. Create the .env file
cp deploy/.env.example deploy/.env
# Edit deploy/.env:
#   - Set IMAGE_TAG=main (or a pinned version)
#   - Set CHAT_PORT if you want something other than 8088
#   - Set CHAT_AUTH_PASSWORD to a strong, random value
```

### Start and verify

```bash
# Start the stack
docker compose -f deploy/docker-compose.yml up -d

# Check that both services are running
docker compose -f deploy/docker-compose.yml ps
# Expected: chat (healthy), watchtower (running)

# Health check
curl http://127.0.0.1:${CHAT_PORT:-8088}/health
# → {"status":"ok"}

# Browser UI (requires Basic auth)
curl -u admin:${CHAT_AUTH_PASSWORD} http://127.0.0.1:${CHAT_PORT:-8088}/
# → HTML page
```

### Stopping

```bash
docker compose -f deploy/docker-compose.yml down
```

______________________________________________________________________

## 4. Claude credentials

The chat server uses `model_level=3` (Claude SDK / Opus), which authenticates via the `claude` CLI's
OAuth token — no API key needed.

### Provisioning on the host

```bash
# Device-flow login
claude login
# → Opens a browser.  Log in with your Anthropic account.
# → After authorisation, ~/.claude/credentials.json is created.

# Verify
ls -la ~/.claude/credentials.json
```

Both the local (`docker-compose.yml`) and deploy (`deploy/docker-compose.yml`) stacks mount
`~/.claude` read-only at `/home/appuser/.claude`. The `claude` CLI inside the container reads the
OAuth token from this file.

### Long-lived OAuth tokens

The `claude login` device flow creates a long-lived OAuth refresh token. If the token expires or is
revoked:

```bash
claude login   # re-authorise
docker compose -f deploy/docker-compose.yml restart chat
```

### Using CLAUDE_CODE_OAUTH_TOKEN (alternative)

If you prefer to pass the token as an environment variable rather than mounting `~/.claude`:

1. Remove (or comment out) the `~/.claude` volume mount in the compose file.
2. Add `CLAUDE_CODE_OAUTH_TOKEN` to the `environment` block.
3. Set the token value in `deploy/.env` or directly in the compose file.

This is an operator decision — the default stack uses the volume mount for simplicity.

______________________________________________________________________

## 5. HTTP Basic Auth

The chat server's built-in HTTP Basic auth protects the browser UI and the `/chat` SSE endpoint. The
`/health` endpoint is exempt (no auth required).

Auth is configured through two layers (the usual cascade applies — env vars override YAML):

| Setting        | YAML path       | Env var         | Default |
| -------------- | --------------- | --------------- | ------- |
| Enable/disable | `auth.enabled`  | `AUTH_ENABLED`  | `false` |
| Username       | `auth.username` | `AUTH_USERNAME` | `admin` |
| Password       | `auth.password` | `AUTH_PASSWORD` | (none)  |

The deploy compose file sets `AUTH_ENABLED=1` and pulls the password from the `.env` file via
`${CHAT_AUTH_PASSWORD:?err}`. The `:?err` syntax causes `docker compose up` to fail with an error if
the variable is unset or empty, preventing accidental deployments without auth.

### Agent instruction

The system prompt (agent instruction) sent to the LLM on every chat turn:

| Setting     | YAML path           | Env var             | Default |
| ----------- | ------------------- | ------------------- | ------- |
| Instruction | `agent.instruction` | `AGENT_INSTRUCTION` | (none)  |

______________________________________________________________________

## 6. Reverse proxy / TLS

> [!NOTE] Provisioning a reverse proxy, a public domain, and TLS certificates is a **manual operator
> step** — no domain, vhost, or certificate configuration is committed to this repo.

The chat server listens on loopback only (`127.0.0.1:${CHAT_PORT}`). To serve it on a public domain,
place a reverse proxy in front of it.

### Example nginx snippet

See the [example nginx reverse proxy snippet](../_snippets/nginx-reverse-proxy.md) for a complete
configuration.

The chat server applies its own HTTP Basic auth — the reverse proxy does **not** need to add a
second auth layer.

For Caddy, Traefik, or other proxies, follow the same pattern: terminate TLS at the proxy and
forward to the loopback port.

______________________________________________________________________

## Conversation history across restarts

The server persists each completed chat exchange to `.data/conversations.json` (one write per turn).
On startup, any saved conversations are loaded back into memory, so a user's prior turns are
restored even after a full container restart — provided the `.data` directory lives on a persistent
volume mount (see [volume mounts](#prerequisites) above).

Key characteristics:

- **Cap**: the most recent 50 turns per conversation are retained (older turns are trimmed).
- **Idle timeout**: when the browser tab has been idle for the configured window (default 30
  minutes), an inline italic notice is appended to the chat but **all prior messages remain
  visible** — the chat area is never cleared. After timeout, the next message starts a fresh
  conversation (new trace session, empty history), but the user can still scroll back through the
  prior exchange.
- **UI reload**: the client id is stored in `localStorage`, and on page load the UI fetches
  `/history?client_id=...` to restore message bubbles. This works regardless of whether the server
  persisted to disk (the in-memory store is sufficient for reloads within the same process
  lifetime).
- **Container restart**: history loaded from `.data/conversations.json` is fully functional —
  idle-reset behaviour, the 50-turn cap, and LRU eviction all apply to restored conversations.

## 7. Updating

### Watchtower (automatic)

With `IMAGE_TAG=main` in `deploy/.env`, Watchtower polls GHCR every 30 seconds. When a new
`main`-tagged image is detected, Watchtower pulls it, stops the old container, and starts a new one
with the same configuration. No manual intervention is needed.

To monitor Watchtower:

```bash
docker compose -f deploy/docker-compose.yml logs watchtower
```

### Manual update

To update to a specific version:

```bash
# Edit deploy/.env and set IMAGE_TAG=v1.2.3
docker compose -f deploy/docker-compose.yml up -d
```

Docker Compose will pull the new tag (if not already cached) and recreate the `chat` container.
