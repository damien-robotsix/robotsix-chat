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

`v*` tags are cut by the shared **auto-release** workflow (weekly + on demand) via release-please —
versions are not tagged by hand.

There is no CI-to-deploy automation — the release workflow only publishes images. central-deploy
pulls them when the operator triggers an update.

______________________________________________________________________

## 3. Deploying via central-deploy

Production deployment is handled by the central-deploy dashboard; there is nothing to
`docker compose up` on the server.

1. Onboard the repo in the dashboard — preflight parses `deploy/docker-compose.yml`
   (`# central-deploy-contract-version: 1`) plus the config template (`config/config.json` +
   `config/config.schema.json`) and renders a typed config form.
1. Fill the config form (secrets are masked), acknowledge that `chat-data` starts empty, confirm the
   Claude-mount toggle, and deploy. Set `server_host` to `0.0.0.0` and `server_port` to `8080` so
   the container serves the published port.
1. Authenticate Claude through central-deploy's **dashboard login flow**, which runs `claude login`
   into the managed `claude-auth` volume (mounted at `/home/app/.claude`). No host `~/.claude` is
   involved.

Verify from the server:

```bash
docker ps --filter name=robotsix-chat   # healthy
curl http://<container>:8080/health     # via the central-deploy network
```

______________________________________________________________________

## 4. Chat-agent mutation access (`allow_chat_access`)

The chat agent can inspect and mutate managed services through the deploy-lifecycle API (see the
[lifecycle skill](../lifecycle/skill.md)). All mutation endpoints — including
`POST /chat/services/{name}/restart` (service restart), `PUT /chat/services/{name}/config`
(config-write), and `POST /chat/services` (service registration) — are gated by a **per-repo access
toggle** in the central-deploy dashboard. When the toggle is off, every mutation attempt returns
`403 Forbidden`.

> This toggle is **not** a chat-component config key or environment variable — it lives in the
> central-deploy management plane. There is nothing to set in `config/config.json` or in the
> container environment.

### Symptom

When the chat agent attempts a mutation (e.g. registering a new service via `POST /chat/services`)
and the toggle is disabled, the agent receives a 403 error. The agent treats this as "not permitted"
and does not retry — the operation fails silently from the operator's perspective.

### Enabling the toggle

1. Open the **central-deploy dashboard**.
1. Navigate to the **repo** (component) whose chat agent needs mutation access.
1. Locate the **"Allow chat access"** (also labelled `chat_agent_mutatable` or `allow_chat_access`)
   setting in the per-repo configuration.
1. Enable it, then save (or redeploy) the repo configuration.

After the toggle is enabled, the chat agent can successfully call mutation endpoints — no restart of
the chat component is required; the deploy server enforces the gate on every request.

______________________________________________________________________

## 5. Claude credentials

The chat server defaults to `model_level=3` (Claude SDK / Opus; level 4 = frontier), which
authenticates via the `claude` CLI's OAuth token — no API key needed.

- **Local dev**: `claude login` on your machine; the root compose bind-mounts `~/.claude` read-only
  at `/home/app/.claude`.
- **Production**: authenticate via central-deploy's dashboard login flow into the managed
  `claude-auth` named volume. If the token expires or is revoked, re-run the dashboard login flow
  and restart the component from the dashboard.

______________________________________________________________________

## 6. Authentication

The chat server ships **no authentication of its own** (robotsix-standards component standard): in
production it is served exclusively through the central-deploy gateway, which validates the
operator's session on every proxied HTTP/WS request. Deployed any other way (own reverse proxy,
exposed port), authentication is the operator's responsibility — put auth at the proxy; never expose
the server directly to an untrusted network.

______________________________________________________________________

## 7. Reverse proxy / TLS

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

## 8. Updating

Every push to `main` publishes a fresh `ghcr.io/damien-robotsix/robotsix-chat:main` (CI-gated).
Redeploy from the central-deploy dashboard to pull it; central-deploy recreates the container with
the stored config and secrets.

______________________________________________________________________

## 9. Mobile SSO (tinyauth)

The [robotsix-chat-mobile](https://github.com/damien-robotsix/robotsix-chat-mobile) app
authenticates through a tinyauth-based SSO flow. Two endpoints serve the handshake — see
[Mobile SSO endpoints](../api/mobile-sso.md) for the full API contract and
[Mobile Auth (SSO)](../configuration.md#mobile-auth-sso) for config keys.

### 9.1 Public edge configuration

The reverse proxy must route two paths to the chat backend with different tinyauth policies:

| Path                           | tinyauth policy | Reason                                                                        |
| ------------------------------ | --------------- | ----------------------------------------------------------------------------- |
| `GET /auth/login`              | **protected**   | User authenticates at the tinyauth edge in a browser; the edge sets identity. |
| `GET /auth/callback`           | **protected**   | Tinyauth redirects here after login; identity headers must be present.        |
| `POST /chat/auth/mobile-token` | **bypass**      | App calls programmatically with only a subject token — no browser session.    |

Everything else follows the existing proxy rules (forward to the chat backend, preserve headers,
support SSE).

#### nginx example with tinyauth

```nginx
server {
    listen 443 ssl;
    server_name chat.example.com;

    ssl_certificate     /etc/letsencrypt/live/chat.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.example.com/privkey.pem;

    # --- tinyauth-protected paths (browser SSO flow) ---
    location /auth/login {
        auth_request /tinyauth;
        auth_request_set $tinyauth_user    $upstream_http_x_forwarded_user;
        auth_request_set $tinyauth_session $upstream_http_x_forwarded_session;

        proxy_pass http://127.0.0.1:8088;
        proxy_set_header X-Forwarded-User    $tinyauth_user;
        proxy_set_header X-Forwarded-Session $tinyauth_session;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto  $scheme;
    }

    location /auth/callback {
        auth_request /tinyauth;
        auth_request_set $tinyauth_user    $upstream_http_x_forwarded_user;
        auth_request_set $tinyauth_session $upstream_http_x_forwarded_session;

        proxy_pass http://127.0.0.1:8088;
        proxy_set_header X-Forwarded-User    $tinyauth_user;
        proxy_set_header X-Forwarded-Session $tinyauth_session;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto  $scheme;
    }

    # --- tinyauth bypass: mobile-token (subject token is the credential) ---
    location /chat/auth/mobile-token {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # No auth_request — the endpoint validates the subject token itself.
    }

    # --- tinyauth internal endpoint ---
    location = /tinyauth {
        internal;
        proxy_pass http://tinyauth:port/auth;  # ← match your tinyauth address
        proxy_set_header X-Original-URI    $request_uri;
        proxy_set_header X-Original-Method $request_method;
    }

    # --- everything else (default: protected by tinyauth or your existing policy) ---
    location / {
        auth_request /tinyauth;
        auth_request_set $tinyauth_user    $upstream_http_x_forwarded_user;
        auth_request_set $tinyauth_session $upstream_http_x_forwarded_session;

        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-User    $tinyauth_user;
        proxy_set_header X-Forwarded-Session $tinyauth_session;
        proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto  $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }
}
```

> **Central-deploy note:** Under central-deploy the gateway handles routing and tinyauth integration
> automatically. The operator configures `mobile_auth.*` keys in the config form; no manual nginx
> edits are needed. The snippet above is for standalone deployments that manage their own reverse
> proxy.

### 9.2 Tinyauth bypass for `POST /chat/auth/mobile-token`

The mobile app calls `POST /chat/auth/mobile-token` **programmatically** — it has no browser session
and cannot complete a tinyauth redirect. The endpoint must therefore be reachable without an
existing SSO session at the edge.

**Security model:** The gate is the subject token, not tinyauth. The endpoint validates the subject
token (HMAC signature, expiry, single-user binding) before issuing an access token. An attacker
without a valid subject token gets `401`; a valid subject token proves the holder previously
authenticated through tinyauth (the only way to obtain one).

**What the edge must do:**

1. Bypass tinyauth for the exact path `/chat/auth/mobile-token` (see the `location` block above).
1. Do **not** inject identity headers on this path — the endpoint derives identity from the subject
   token, not from edge headers.
1. Forward the request body unchanged (the app sends `{"token": "<subject-token>"}`).

**What the edge must NOT do:**

- Do not require a browser cookie or SSO session for this path.
- Do not strip or rewrite the `Authorization` header if one is present.

### 9.3 Token lifecycle

#### Subject token

| Property   | Value                                                                                |
| ---------- | ------------------------------------------------------------------------------------ |
| Issued by  | `GET /auth/login` (via the tinyauth callback flow)                                   |
| Format     | Opaque string: `{subject}\|{expiry_unix}\|{hmac_sha256_tag}`                         |
| Binding    | Single user (the tinyauth identity at issuance time)                                 |
| Lifetime   | Controlled by `mobile_auth.token_ttl_seconds` (default 3600 s / 1 h)                 |
| Storage    | Cached by the mobile app on the device                                               |
| Revocation | Rotate `mobile_auth.token_secret` — all outstanding tokens are invalidated instantly |

The subject token is HMAC-signed with `mobile_auth.token_secret`. The server verifies the signature
and expiry on every exchange call without storing token state — this makes issuance and verification
stateless. Rotating the secret revokes all outstanding subject tokens at once (the app prompts
re-login on the next `401`).

#### Access token

| Property    | Value                                                                     |
| ----------- | ------------------------------------------------------------------------- |
| Issued by   | `POST /chat/auth/mobile-token`                                            |
| Format      | Same HMAC-signed format as the subject token                              |
| Binding     | Single user (same subject as the subject token)                           |
| Lifetime    | Controlled by `mobile_auth.token_ttl_seconds` (default 3600 s / 1 h)      |
| Storage     | Cached by the mobile app; discarded on expiry                             |
| Re-exchange | App silently re-exchanges the subject token when the access token expires |

#### Expiry behaviour

1. The app caches the access token until `expires_in` seconds elapse.
1. On expiry, the app re-exchanges the cached subject token for a fresh access token (silent, no
   user interaction).
1. If the exchange returns `401` (subject token expired, invalid, or revoked), the app clears the
   stored subject token and prompts the user to log in again via `GET /auth/login`.

### 9.4 Monitoring and debugging

#### Key metrics to watch

| Metric / signal                    | Where to log                        | What it tells you                                                                     |
| ---------------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------- |
| `auth_login` log entries           | Chat backend logs (structured JSON) | Login flow initiation rate; `redirect_to` values                                      |
| `auth_callback` log entries        | Chat backend logs                   | Successful tinyauth callbacks (user completed login)                                  |
| `mobile_token` log entries         | Chat backend logs                   | Token issuance rate; subject values                                                   |
| `401` on `/chat/auth/mobile-token` | Reverse proxy access logs           | Failed exchanges — expected on expiry; spikes indicate revocation or misconfiguration |
| `400` on `/auth/login`             | Reverse proxy access logs           | Bad `redirect_to` values — possible app bug or attack                                 |
| `404` on auth endpoints            | Reverse proxy access logs           | Endpoints disabled (`mobile_auth.enabled: false`) or image too old                    |

#### Common failure modes

| Symptom                                                                       | Likely cause                                                                       | Fix                                                                   |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `GET /auth/login` returns JSON `{"error": "not found"}`                       | `mobile_auth.enabled` is `false` or the running image predates the feature         | Set `enabled: true` and redeploy from a current image                 |
| `GET /auth/login` returns `400` "redirect_to domain is not in the allowlist"  | `redirect_to` host is not in `allowed_redirect_domains`                            | Add the app's callback domain to the allowlist                        |
| `GET /auth/login` returns `401` "missing identity header"                     | Tinyauth did not set `X-Forwarded-User` — the edge is not passing through tinyauth | Check edge config: `/auth/login` must be tinyauth-protected           |
| `POST /chat/auth/mobile-token` returns `401` "missing identity header"        | The edge is applying tinyauth to this path (should be bypassed)                    | Add a tinyauth bypass `location` block for `/chat/auth/mobile-token`  |
| `POST /chat/auth/mobile-token` returns `401` on valid subject token           | Subject token expired or `token_secret` was rotated                                | App will prompt re-login; if unintended, check secret rotation timing |
| `POST /chat/auth/mobile-token` returns `500` "token_secret is not configured" | `mobile_auth.token_secret` is empty                                                | Set a strong random secret in the config                              |
| App shows tinyauth login page instead of redirecting back                     | `callback_base_url` is wrong or the tinyauth callback URL is misconfigured         | Verify `callback_base_url` matches the public URL tinyauth can reach  |

#### Debugging steps

1. **Verify the endpoints are live:**

   ```bash
   # Should return 400 (missing redirect_to), NOT 404
   curl -s -o /dev/null -w '%{http_code}\n' https://chat.example.com/auth/login

   # Should return 401 (missing identity header), NOT 404
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://chat.example.com/chat/auth/mobile-token
   ```

1. **Verify tinyauth is setting identity headers:**

   ```bash
   # After authenticating at tinyauth, check the forwarded header
   curl -s -H 'X-Forwarded-User: testuser' -X POST https://chat.example.com/chat/auth/mobile-token
   # Should return 200 with a token JSON body
   ```

1. **Check structured logs** — all auth endpoints log at `INFO` level with `subject`, `redirect_to`,
   and `ttl` fields. Filter for `auth_login`, `auth_callback`, or `mobile_token` in the log stream.

### 9.5 Rollback / disable

Mobile SSO can be disabled at two levels:

#### Config-level disable (preferred)

Set `mobile_auth.enabled: false` in the config file and restart the chat backend. The endpoints
remain registered but return `404` on every request — the app treats this as "SSO not available" and
falls back gracefully.

This is the recommended approach for temporary disabling (e.g. during an incident) because it
requires no edge changes and can be reversed by setting `enabled: true` and restarting.

#### Edge-level disable

Remove or comment out the tinyauth-protected `location /auth/login` and `location /auth/callback`
blocks in the reverse proxy config and reload nginx. This makes the endpoints unreachable from the
public internet regardless of the backend config.

For a complete rollback (backend + edge):

1. Set `mobile_auth.enabled: false` in the config.
1. Restart the chat backend.
1. Remove the mobile SSO `location` blocks from the reverse proxy.
1. Reload the reverse proxy.

Under central-deploy, toggle `mobile_auth.enabled` in the config form and redeploy — the gateway
routing is managed automatically.
