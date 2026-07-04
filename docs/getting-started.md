# Getting Started

## Prerequisites

- **Python 3.14+** and [uv](https://docs.astral.sh/uv/)
- **Claude CLI** (for model levels 3–4, the default) —
  [install guide](https://docs.anthropic.com/en/docs/claude-code/overview)

## Quick start

1. **Install** from a checkout (uv resolves the git-pinned first-party dependencies; pip is not a
   supported install path):

   ```bash
   git clone https://github.com/damien-robotsix/robotsix-chat
   cd robotsix-chat
   uv sync --extra claude-sdk
   ```

   Available extras: `claude-sdk` (default levels 3–4 transport), `openrouter` (levels 1–2),
   `tracing` (Langfuse), `memory` (cognee persistence).

2. **Authenticate the Claude CLI** (one-time, subscription auth — no API key):

   ```bash
   claude login
   ```

3. **Run the server** — the committed `config/config.json` defaults (model level 3) work out of the
   box:

   ```bash
   uv run robotsix-chat
   ```

   To override settings or add credentials, copy the template to the gitignored
   `config/config.local.json` and point the file locator at it:

   ```bash
   cp config/config.json config/config.local.json
   ROBOTSIX_CONFIG_FILE=config/config.local.json uv run robotsix-chat
   ```

4. **Open your browser** at [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Configuration

See the [Configuration](configuration.md) page for a full reference of every setting, including
OpenRouter API keys, memory, mail, and refdocs integration.

## Deployment

See the [Deployment](user-guide/deployment.md) guide — production runs through
[central-deploy](https://github.com/damien-robotsix/robotsix-central-deploy) via the
`deploy/docker-compose.yml` contract.
