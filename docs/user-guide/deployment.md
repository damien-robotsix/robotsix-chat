# Deployment

End-to-end guide: build locally, publish to GHCR, deploy the pull-based Docker Compose stack, and
reach the loopback service.

______________________________________________________________________

## Overview

robotsix-chat ships as a single Docker image published to `ghcr.io/damien-robotsix/robotsix-chat`.
There are two Compose files:

| File                             | Purpose                                               |
| -------------------------------- | ----------------------------------------------------- |
| `docker-compose.yml` (repo root) | Local build-and-run loop for development              |
| `deploy/docker-compose.yml`      | central-deploy contract for the production deployment |

Production runs under
[robotsix-central-deploy](https://github.com/damien-robotsix/robotsix-central-deploy): it pulls the
published image, injects operator-filled secrets, manages restarts and networking, and routes the
service through its gateway. See `deploy/README.md` for the onboarding walkthrough.

______________________________________________________________________

## 1. Local build and run

The repo-root `docker-compose.yml` builds the image from source and starts a single `chat`
container. Use this for development, testing, or ad-hoc runs.

### Prerequisites

- Docker Engine 24+ and Compose v2
- Claude subscription: `claude login` (populates `~/.claude`)
- **Persistent `/data` volume**: conversation history is written to `/data/conversations.json`
  inside the container. In production the named volume `chat-data` is mounted at `/data`
  (read-write) so chat history survives redeploys.

### Steps

```bash
# 1. Create a local config file
cp config/chat.local.example.yaml config/chat.local.yaml


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
# automatically and updates the `main` tag on GHCR.  Redeploy from the
# central-deploy dashboard to pick it up.
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

## 3. Deploying via central-deploy

Production deployment is handled by the central-deploy dashboard; there is nothing to
`docker compose up` on the server.

1. One-time: `claude login` as the server user (populates `~/.claude`, which central-deploy binds
   into the container at `/home/app/.claude`).
2. Onboard the repo in the dashboard — preflight parses `deploy/docker-compose.yml`
   (`# central-deploy-contract-version: 1`).
3. Fill any secret slots you need (memory keys), acknowledge the `chat-data` stateful-volume
   warning, confirm the Claude-mount toggle, and deploy.

Verify from the server:

```bash
docker ps --filter name=robotsix-chat   # healthy
curl http://<container>:8080/health     # via the central-deploy network
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

The local stack (`docker-compose.yml`) bind-mounts `~/.claude` read-only at `/home/app/.claude`; in
production the `robotsix.deploy.claude-mount` label makes central-deploy bind it there read-write.
The `claude` CLI inside the container reads the OAuth token from this directory.

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

## 5. Authentication

The chat server ships **no authentication of its own** (robotsix-standards component standard): in
production it is served exclusively through the central-deploy gateway, which validates the
operator's session on every proxied HTTP/WS request. Deployed any other way (own reverse proxy,
exposed port), authentication is the operator's responsibility — put auth at the proxy; never expose
the server directly to an untrusted network.

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

The server persists each completed chat exchange to `/data/conversations.json` (one write per turn).
On startup, any saved conversations are loaded back into memory, so a user's prior turns are
restored even after a full container restart — provided the `/data` directory lives on a persistent
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
- **Container restart**: history loaded from `/data/conversations.json` is fully functional —
  idle-reset behaviour, the 50-turn cap, and LRU eviction all apply to restored conversations.

## 7. Updating

Every push to `main` publishes a fresh `ghcr.io/damien-robotsix/robotsix-chat:main` (CI-gated).
Redeploy from the central-deploy dashboard to pull it; central-deploy recreates the container with
the stored config and secrets.
