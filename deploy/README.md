# robotsix-chat — deploy stack

Pull-based Docker Compose stack that runs robotsix-chat behind HTTP Basic
auth, self-updating via Watchtower.

## Architecture

```
docker compose -f deploy/docker-compose.yml up -d

┌──────────────────────────────────────────────┐
│  Host (loopback only)                        │
│                                              │
│  127.0.0.1:${CHAT_PORT}  ──►  chat:8080     │
│                                              │
│  chat (ghcr.io/damien-robotsix/robotsix-chat)│
│  ├─ config/  ← deploy/config/chat.local.yaml │
│  ├─ .data/   ← deploy/data (persistent)      │
│  └─ .claude/ ← ~/.claude (read-only)         │
│                                              │
│  watchtower (containrrr/watchtower)          │
│  └─ polls GHCR every 30 s for a new image    │
└──────────────────────────────────────────────┘
```

The stack binds **only to loopback** — the chat server is never exposed to
the public internet directly.  Operators are expected to place a reverse
proxy (nginx, Caddy, …) in front of it; see [Reverse proxy](#reverse-proxy--tls)
below for a placeholder snippet.

## Quick start

```bash
# 1. One-time host prerequisites
claude login                                    # populates ~/.claude
mkdir -p deploy/config deploy/data              # data/ = persistent agent storage
cp config/chat.local.example.yaml deploy/config/chat.local.yaml
# Edit deploy/config/chat.local.yaml and set auth.enabled: true

# 2. Set secrets
cp deploy/.env.example deploy/.env
# Edit deploy/.env: set CHAT_AUTH_PASSWORD to a strong value

# 3. Start
docker compose -f deploy/docker-compose.yml up -d

# 4. Verify
curl -u admin:${CHAT_AUTH_PASSWORD} http://127.0.0.1:${CHAT_PORT}/health
```

## Prerequisites

| Requirement | How to satisfy |
|---|---|
| Docker Engine 24+ and Compose v2 | `docker compose version` |
| Claude subscription credentials | `claude login` (see [Claude credentials](#claude-credentials)) |
| Config file | `deploy/config/chat.local.yaml` (copy from `config/chat.local.example.yaml`) |
| `.env` file | `deploy/.env` populated from `deploy/.env.example` |

## .env reference

Copy `deploy/.env.example` to `deploy/.env` and fill in the values:

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMAGE_TAG` | yes | (none in example) | GHCR image tag to pull — `main` for continuous deploy, `v1.2.3` for a release, or a full commit SHA |
| `CHAT_PORT` | no | `8088` | Loopback port the chat server is exposed on |
| `CHAT_AUTH_PASSWORD` | **yes** | (none) | HTTP Basic Auth password; the username defaults to `admin` |

The compose file uses `${CHAT_AUTH_PASSWORD:?err}` — if the variable is unset or
empty, `docker compose up` refuses to start.

## Claude credentials

The chat server's `model_level=3` transport uses the `claude` CLI (keyless
OAuth) to authenticate with Anthropic.  The container mounts `~/.claude`
read-only, so the host must have valid Claude subscription credentials.

### Provisioning (one-time, on the host)

```bash
# Device-flow login — opens a browser for you to authorise.
claude login

# Verify the credentials file exists.
ls -la ~/.claude/credentials.json
```

The OAuth token in `credentials.json` is long-lived.  If the token expires
(Anthropic revokes it or the subscription lapses), re-run `claude login`.

### Using CLAUDE_CODE_OAUTH_TOKEN (alternative)

If you prefer not to mount the entire `~/.claude` directory, you can provide the
OAuth token directly as an environment variable.  This requires modifying the
compose file to pass `CLAUDE_CODE_OAUTH_TOKEN` and removing the `~/.claude`
volume mount — both one-line edits to `deploy/docker-compose.yml`.

## Starting the stack

```bash
# From the repo root:
docker compose -f deploy/docker-compose.yml up -d

# Check status:
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs chat
```

The `chat` service has a built-in healthcheck (probes `/health` every 30 s).
Wait for the status to show `(healthy)` before relying on the service.

## Verifying the service

```bash
# Health check (no auth required)
curl http://127.0.0.1:${CHAT_PORT:-8088}/health
# → {"status":"ok"}

# Chat endpoint (requires Basic auth)
curl -u admin:${CHAT_AUTH_PASSWORD} http://127.0.0.1:${CHAT_PORT:-8088}/
# → HTML browser UI

# Unauthenticated requests to /chat are rejected with 401
curl -i http://127.0.0.1:${CHAT_PORT:-8088}/chat
# → HTTP/1.1 401 Unauthorized
```

## Watchtower — automatic updates

The stack includes [Watchtower](https://containrrr.dev/watchtower/) configured to:

- Poll GHCR every 30 seconds for a new image under the `main` tag
- Only update containers with the `com.centurylinklabs.watchtower.enable=true` label
- Clean up old images after pulling a new one

When the release-image workflow pushes a new `main`-tagged image, Watchtower
pulls it, stops the old container, and starts a new one with the same
configuration — zero-downtime in practice because the restart is near-instant.

To pin a specific version and disable auto-updates, set `IMAGE_TAG=v1.2.3`
(or a commit SHA) in `deploy/.env` and remove the `watchtower` service from
the compose file.

## Reverse proxy / TLS

> [!NOTE]
> This is a **placeholder** — provisioning a reverse proxy, a public domain,
> and TLS certificates is a manual operator step outside this repo's scope.

The chat server binds to loopback only.  To expose it on a public domain,
place nginx (or Caddy, Traefik, …) in front of it.  See the [example nginx reverse proxy snippet](../docs/_snippets/nginx-reverse-proxy.md) for a complete configuration.

The chat server applies its own HTTP Basic auth — the reverse proxy does
**not** need to add a second auth layer (but may if desired).

## Troubleshooting

| Symptom | Check |
|---|---|
| `docker compose up` fails with "CHAT_AUTH_PASSWORD is required" | `deploy/.env` is missing or `CHAT_AUTH_PASSWORD` is empty |
| Container exits immediately | `docker compose logs chat` — likely a missing config file or invalid YAML |
| `/chat` returns 500 or empty responses | Claude credentials are missing or expired; check `~/.claude/credentials.json` on the host, re-run `claude login` if needed |
| Watchtower isn't updating | Check `docker compose logs watchtower`; ensure the `chat` container has the `com.centurylinklabs.watchtower.enable=true` label |
