# robotsix-chat

**Browser + SSE chat server for an LLM agent** — exposes an LLM agent to human users over HTTP, with
a self-contained browser chat UI.

`robotsix-chat` drives an LLM through
[`robotsix-llmio`](https://github.com/damien-robotsix/robotsix-llmio) (pick a `model_level`, never a
concrete provider), then serves it over Server-Sent Events:

- `GET /` — a single-file browser chat UI (no build step, no framework).
- `POST /chat` — accepts `{"message": "..."}` and returns the agent's reply as SSE frames.
- `GET /health` — liveness probe.

The UI and the API are served from the **same origin** by default, so the browser talks to `/chat`
with no CORS configuration. When you host the UI separately, set `CORS_ALLOW_ORIGINS`.

> This package was split out of
> [`robotsix-agent-comm`](https://github.com/damien-robotsix/robotsix-agent-comm): the chat server
> never depended on that project's message protocol / transport, so it now lives on its own.
> `robotsix-agent-comm` is back to being a stdlib-only agent-to-agent communication stack.

## Quick start

```bash
git clone https://github.com/damien-robotsix/robotsix-chat.git
cd robotsix-chat
uv sync
```

### Run against a real LLM

The LLM is selected through [`robotsix-llmio`](https://github.com/damien-robotsix/robotsix-llmio):
you pick a **model level** (1–3) and llmio resolves the provider + model for it — you never name a
concrete provider. By default level 3 → Claude (`claudeSDK-opus`), levels 1–2 → OpenRouter
(deepseek).

Easiest path — level 3, the Claude Agent SDK, which uses your `claude login` subscription, so **no
API key**:

```bash
uv sync --extra claude-sdk          # pulls claude-agent-sdk via robotsix-llmio
claude login                        # one-time; also needs Node.js on PATH
cp config/chat.local.example.yaml config/chat.local.yaml   # defaults to model_level 3
uv run robotsix-chat
```

Open <http://127.0.0.1:8000/> in your browser and start chatting.

Prefer a cheaper level (1–2, OpenRouter deepseek)? Install that extra and set the key (env vars
override the config file; a `.env` file is picked up too):

```bash
uv sync --extra openrouter
export LLMIO_MODEL_LEVEL=1           # 1 cheapest · 2 · 3 best
export LLMIO_API_KEY=sk-or-...
uv run robotsix-chat
```

### Run without an API key (echo agent)

To exercise the UI with no LLM credentials, point the server at a trivial echo agent. Create
`demo.py`:

```python
import asyncio
from collections.abc import AsyncIterator

from robotsix_chat.chat.server import run_server


class EchoAgent:
    """Echo each word back, one token at a time."""

    async def stream(self, message: str) -> AsyncIterator[str]:
        for word in message.split():
            await asyncio.sleep(0.15)
            yield f"{word} "


if __name__ == "__main__":
    run_server(EchoAgent(), host="127.0.0.1", port=8000)
```

```bash
uv run python demo.py
```

Then talk to it over `curl` to see the raw SSE stream:

```bash
curl -N -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello agent world"}'
```

```
data: {"type": "token", "content": "Hello "}

data: {"type": "token", "content": "agent "}

data: {"type": "token", "content": "world "}

data: {"type": "done"}
```

## Configuration

Settings resolve through a layered cascade that matches the rest of the robotsix stack
(`robotsix-mill`, `robotsix-auto-mail`), built on the shared
[`robotsix-yaml-config`](https://github.com/damien-robotsix/robotsix-yaml-config) library:

```
pydantic field defaults  →  YAML config file  →  environment variables
```

with each later layer overriding the earlier one **field-by-field**.

### Config file

The YAML file lives at **`config/chat.local.yaml`** by default — copy it from the committed
[`config/chat.local.example.yaml`](config/chat.local.example.yaml). It is git-ignored so credentials
never land in the repo. Override the path with the `CHAT_CONFIG_PATH` environment variable.

```yaml
llmio:                       # LLM selection, delegated to robotsix-llmio
  model_level: 3             # 1 (cheapest) | 2 | 3 (most capable)
  # api_key: sk-or-...       # required for levels 1-2 (OpenRouter); level 3 needs none

agent:
  # instruction: You are a helpful assistant.

server:
  # host: 127.0.0.1
  # port: 8000
  # log_level: INFO
  # cors_allow_origins: []   # ["*"] = any; only when the UI is hosted elsewhere

auth:                        # HTTP Basic Auth gating the UI and /chat
  enabled: false
  username: admin
  password: ""               # required when enabled
```

### Model level

The LLM is configured the [`robotsix-llmio`](https://github.com/damien-robotsix/robotsix-llmio) way
— you pick a capability **level** (1–3) and llmio resolves the provider + model for it (via
`robotsix_llmio.config.create_model`). robotsix-chat never names a concrete provider or model. The
default level → provider-model mapping:

| `model_level`    | provider-model identifier               | needs API key?         |
| ---------------- | --------------------------------------- | ---------------------- |
| 1 (cheapest)     | `openrouter-deepseek/deepseek-v4-flash` | yes (`llmio.api_key`)  |
| 2                | `openrouter-deepseek/deepseek-v4-pro`   | yes (`llmio.api_key`)  |
| 3 (most capable) | `claudeSDK-opus`                        | no (subscription auth) |

- **Level 3 / `claudeSDK`** — the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
  authenticates via your local `claude login` subscription, so **no API key**. Install with
  `uv sync --extra claude-sdk` and run `claude login` (needs Node.js on PATH).
- **Levels 1–2 / `openrouter`** — install with `uv sync --extra openrouter` and set `llmio.api_key`
  (env `LLMIO_API_KEY`).

Each backend dependency is pulled **through** robotsix-llmio's own extras
(`robotsix-llmio[claude-sdk]` / `robotsix-llmio[openrouter]`), so the stack owns those deps in one
place.

> Replies are returned as a single block (not token-streamed): llmio's Claude SDK model does not yet
> support incremental streaming through pydantic-ai.

### Login (authentication)

Set `auth.enabled: true` (with a `password`) to gate the browser UI and the `/chat` endpoint behind
HTTP Basic Auth. The `GET /health` probe stays open. The browser prompts for credentials on first
load and reuses them for the chat requests, so no UI changes are needed. **Enable this for any
deployment reachable beyond `localhost`.**

### Environment variables

Every field can also be set via an environment variable (with `.env` support); env vars override the
config file.

| Variable             | Config key                  | Default                        | Description                                                                                                            |
| -------------------- | --------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `LLMIO_MODEL_LEVEL`  | `llmio.model_level`         | `3`                            | Capability level: `1` (cheapest), `2`, or `3` (most capable).                                                          |
| `LLMIO_API_KEY`      | `llmio.api_key`             | *(required for levels 1–2)*    | OpenRouter API key (unused by level 3 / claudeSDK).                                                                    |
| `AGENT_INSTRUCTION`  | `agent.instruction`         | `You are a helpful assistant.` | System instruction for the agent.                                                                                      |
| `SERVER_HOST`        | `server.host`               | `127.0.0.1`                    | Host the server binds to.                                                                                              |
| `SERVER_PORT`        | `server.port`               | `8000`                         | Port the server listens on.                                                                                            |
| `LOG_LEVEL`          | `server.log_level`          | `INFO`                         | Python logging level.                                                                                                  |
| `CORS_ALLOW_ORIGINS` | `server.cors_allow_origins` | *(empty)*                      | Comma-separated origins allowed to call `/chat` cross-origin (`*` = any). Only needed when the UI is hosted elsewhere. |
| `AUTH_ENABLED`       | `auth.enabled`              | `false`                        | Gate the UI and `/chat` behind HTTP Basic Auth.                                                                        |
| `AUTH_USERNAME`      | `auth.username`             | `admin`                        | Accepted username.                                                                                                     |
| `AUTH_PASSWORD`      | `auth.password`             | *(empty)*                      | Accepted password (required when auth is enabled).                                                                     |

### Calendar & tasks (broker integration)

The assistant can read your calendar (events, availability/free-busy) and manage your personal tasks
via the [`robotsix-calendar-agent`](https://github.com/damien-robotsix/robotsix-calendar-agent)
broker. When enabled, the agent gains four tools:

| Tool              | What it does                                           |
| ----------------- | ------------------------------------------------------ |
| `query_calendar`  | Read your schedule, upcoming events, and availability. |
| `manage_calendar` | Create, update, or cancel calendar events.             |
| `query_tasks`     | List your to-dos.                                      |
| `manage_tasks`    | Create, update, complete, or delete tasks.             |

The integration is **off by default**. To enable it:

```bash
uv sync --extra broker
```

Then set in your `.env` (or YAML config):

```bash
CALENDAR_ENABLED=true
CALENDAR_BROKER_TOKEN=<your-agent-token>   # required when enabled
# CALENDAR_BROKER_HOST=ai-broker.robotsix.net   # default
```

All settings are listed in the
[Calendar (broker integration)](docs/configuration.md#calendar-broker-integration) section of
`docs/configuration.md`.

## Application factory

`create_app(agent, *, serve_ui=True, cors_allow_origins=None, auth=None)` returns a Starlette ASGI
app you can mount in tests via `httpx.ASGITransport` or run with `uvicorn`. Pass a
`robotsix_chat.chat.auth.BasicAuthConfig` as `auth` to gate every request except `GET /health`. Any
object with an `async def stream(self, message: str) -> AsyncIterator[str]` method satisfies the
`ChatAgent` protocol.

## Development

```bash
make install                     # uv sync --all-extras
make lint                        # ruff check
make format-check                # ruff format --check
make typecheck                   # mypy (strict)
make test                        # pytest
make all                         # lint + format-check + typecheck + test
```

Or use the raw `uv run` commands directly — the `Makefile` targets are thin wrappers with no hidden
logic:

```bash
uv sync
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run mypy .                    # static type checking (strict)
uv run pytest                    # tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full contributor setup, including pre-commit hooks and
dependency auditing.

Task tracking lives under [`tasks/`](tasks/) — see [`tasks/README.md`](tasks/README.md) for the
format and workflow.

## License

MIT — see [`LICENSE`](LICENSE).
