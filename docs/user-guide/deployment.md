# Deployment

End-to-end guide: build locally, publish to GHCR, and deploy through central-deploy.

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
published image, writes the operator-managed config into the `chat-config` volume, manages restarts
and networking, and routes the service through its gateway. See `deploy/README.md` for the
onboarding walkthrough.

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
# 1. Create a local config file (config standard: one JSON file)
cp config/config.json config/config.local.json
# Edit config.local.json: server_host "0.0.0.0", server_port 8080, plus any credentials.

# 2. Build and start
docker compose up --build

# The chat server is now reachable at http://127.0.0.1:8080
```

The compose file mounts `config/config.local.json` (at the container's
`/home/app/config/config.json`) and `~/.claude` read-only. The only environment variable is the
config-file locator `ROBOTSIX_CONFIG_FILE` — all settings live in the config file.

To stop: `docker compose down`.

______________________________________________________________________

## 2. Publishing to GHCR

The
[release-image workflow](https://github.com/damien-robotsix/robotsix-chat/blob/main/.github/workflows/release-image.yml)
calls the fleet's shared `docker-release.yml` to build and push the image to
`ghcr.io/damien-robotsix/robotsix-chat`. It triggers on:

| Trigger                       | Tags pushed            |
| ----------------------------- | ---------------------- |
| Push to `main`                | `main`, `sha-<short>`  |
| Push of a `v*` tag (`v1.2.3`) | `1.2.3`, `sha-<short>` |
| Manual (`workflow_dispatch`)  | same as branch/tag     |

Every build also produces provenance and SBOM attestations, and a Trivy publish gate blocks on
fixable CRITICAL findings. There is no `latest` tag.

`v*` tags are cut by the shared **auto-release** workflow (weekly + on demand) from the
`changelog.d/` fragments — versions are not tagged by hand.

There is no CI-to-deploy automation — the release workflow only publishes images. central-deploy
pulls them when the operator triggers an update.

______________________________________________________________________

## 3. Deploying via central-deploy

Production deployment is handled by the central-deploy dashboard; there is nothing to
`docker compose up` on the server.

1. Onboard the repo in the dashboard — preflight parses `deploy/docker-compose.yml`
   (`# central-deploy-contract-version: 1`) plus the config template (`config/config.json` +
   `config/config.schema.json`) and renders a typed config form.
2. Fill the config form (secrets are masked), acknowledge that `chat-data` starts empty, confirm the
   Claude-mount toggle, and deploy. Set `server_host` to `0.0.0.0` and `server_port` to `8080` so
   the container serves the published port.
3. Authenticate Claude through central-deploy's **dashboard login flow**, which runs `claude login`
   into the managed `claude-auth` volume (mounted at `/home/app/.claude`). No host `~/.claude` is
   involved.

Verify from the server:

```bash
docker ps --filter name=robotsix-chat   # healthy
curl http://<container>:8080/health     # via the central-deploy network
```

______________________________________________________________________

## 4. Claude credentials

The chat server defaults to `model_level=3` (Claude SDK / Opus; level 4 = frontier), which
authenticates via the `claude` CLI's OAuth token — no API key needed.

- **Local dev**: `claude login` on your machine; the root compose bind-mounts `~/.claude` read-only
  at `/home/app/.claude`.
- **Production**: authenticate via central-deploy's dashboard login flow into the managed
  `claude-auth` named volume. If the token expires or is revoked, re-run the dashboard login flow
  and restart the component from the dashboard.

______________________________________________________________________

## 5. Authentication

The chat server ships **no authentication of its own** (robotsix-standards component standard): in
production it is served exclusively through the central-deploy gateway, which validates the
operator's session on every proxied HTTP/WS request. Deployed any other way (own reverse proxy,
exposed port), authentication is the operator's responsibility — put auth at the proxy; never expose
the server directly to an untrusted network.

______________________________________________________________________

## 6. Reverse proxy / TLS

> [!NOTE] Provisioning a reverse proxy, a public domain, and TLS certificates is a **manual operator
> step** — no domain, vhost, or certificate configuration is committed to this repo. Under
> central-deploy this is already handled by the gateway.

If you run the server outside central-deploy, bind it to loopback (`server_host` in the config file)
and place a reverse proxy in front of it. The proxy must supply the authentication layer — the
server has none of its own.

### Example nginx snippet

See the [example nginx reverse proxy snippet](../_snippets/nginx-reverse-proxy.md) for a complete
configuration.

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
