# robotsix-chat — production deployment

robotsix-chat deploys through
[robotsix-central-deploy](https://github.com/damien-robotsix/robotsix-central-deploy).
`docker-compose.yml` in this directory is **the deploy contract** central-deploy consumes (first
line: `# central-deploy-contract-version: 1`) — it is not meant to be run with `docker compose`
directly. The root `docker-compose.yml` remains the local-dev stack.

## How it works

- central-deploy **pulls** `ghcr.io/damien-robotsix/robotsix-chat:main` (published by
  `.github/workflows/release-image.yml` on every push to main — gated on green CI and a Trivy scan).
  It never builds.
- Lifecycle is managed by central-deploy: restart policy, networking, gateway routing
  (`deploy.robotsix.net/<component>/*` → container port 8080), and redeploys from the dashboard. No
  Watchtower.
- **Configuration** lives in a single YAML config file mounted by central-deploy.
  `deploy/config.example.yaml` is the committed template — the operator copies it to
  `config/chat.local.yaml`, fills in real values (secret slots, tune defaults), and
  central-deploy injects it into the container via the
  `robotsix.deploy.config-target` label.  The container reads it via the
  `CHAT_CONFIG_PATH` env var (infrastructure wiring only).  No application config
  or secrets live in `environment:`.
- **Persistent state** (knowledge store, cognee memory, HF cache) lives in the named volume
  `chat-data`, mounted at `/home/app/.data` and flagged `robotsix.deploy.stateful` — it starts EMPTY
  on first onboard; migrate data from a previous deployment first if needed.
- **Claude credentials**: the `robotsix.deploy.claude-mount: "true"` label makes central-deploy bind
  the server user's `~/.claude` to `/home/app/.claude` (the standardized container user's home),
  enabling the keyless level-3 claude-sdk transport. Run `claude login` as the server user once
  beforehand.

## Onboarding

1. In the central-deploy dashboard, start onboarding and point it at this repository. Preflight
   parses `deploy/docker-compose.yml`.
2. Fill the config file: copy `deploy/config.example.yaml` to `config/chat.local.yaml` and fill
   in the secret slots (at minimum `auth.password`).  central-deploy injects this file into the
   container at the path declared by the `robotsix.deploy.config-target` label.
3. Acknowledge the `chat-data` stateful-volume warning (empty on first deploy) and confirm the
   Claude-mount toggle.
4. Deploy.

## Migrating from the old Watchtower stack

The previous stack (`/opt/robotsix-chat`, chat + watchtower services, bind mounts) is superseded. To
carry over agent state, copy the contents of the old `deploy/data/` bind mount into the new
`chat-data` volume before first start, then stop and remove the old compose stack.
