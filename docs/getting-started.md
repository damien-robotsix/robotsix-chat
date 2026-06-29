# Getting Started

## Prerequisites

- **Python 3.14+**
- **Claude CLI** (for model level 3, the default) —
  [install guide](https://docs.anthropic.com/en/docs/claude-code/overview)

## Quick start

1. **Install** the package:

   ```bash
   pip install robotsix-chat
   ```

   Or clone the repo and sync with extras:

   ```bash
   git clone https://github.com/damien-robotsix/robotsix-chat
   cd robotsix-chat
   uv sync --extra claude-sdk
   ```

   Available extras: `claude-sdk` (default level 3 transport), `openrouter` (levels 1–2), `memory`
   (cognee persistence), `broker` (mill integration).

2. **Copy the example config**:

   ```bash
   cp config/chat.local.example.yaml config/chat.local.yaml
   ```

   The defaults work out of the box for model level 3 with no API key.

3. **Run the server**:

   ```bash
   robotsix-chat
   ```

   Or from the repo:

   ```bash
   uv run robotsix-chat
   ```

4. **Open your browser** at [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Configuration

See the [Configuration](configuration.md) page for a full reference of every setting, including
OpenRouter API keys, HTTP Basic Auth, memory, mill, calendar, and refdocs integration.

## Deployment

See the [Deployment](user-guide/deployment.md) guide for Docker, GHCR publishing, Watchtower
auto-updates, and reverse-proxy TLS setup.
