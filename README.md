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
with no CORS configuration. When you host the UI separately, set `cors_allow_origins` in the config
file.

robotsix-chat is a **deployable component** (see the
[robotsix stack standards](https://github.com/damien-robotsix/robotsix-standards)): it ships as a
container image and deploys through central-deploy. Full documentation:
<https://damien-robotsix.github.io/robotsix-chat/>.

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
uv run robotsix-chat                # reads the committed config/config.json defaults (model_level 3)
```

Open <http://127.0.0.1:8000/> in your browser and start chatting.

Prefer a cheaper level (1–2, OpenRouter deepseek)? Install that extra, copy the defaults template to
a gitignored local file, and set the key there (never in the committed template):

```bash
uv sync --extra openrouter
cp config/config.json config/config.local.json
# Edit config/config.local.json: set llmio_model_level to 1 and llmio_api_key
ROBOTSIX_CONFIG_FILE=config/config.local.json uv run robotsix-chat
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
    run_server(EchoAgent(), host="0.0.0.0", port=8000)
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

```text
data: {"type": "token", "content": "Hello "}

data: {"type": "token", "content": "agent "}

data: {"type": "token", "content": "world "}

data: {"type": "done"}
```

## Configuration

Settings are loaded from a single JSON config file via
[`robotsix-config`](https://github.com/damien-robotsix/robotsix-config). The file path is set by the
`ROBOTSIX_CONFIG_FILE` environment variable (the only env var consumed for config).

### Config file

The committed [`config/config.json`](config/config.json) is the **defaults template** (config
standard): it documents every field with its default value, and central-deploy merges operator edits
into it at deploy time. It must never contain real credentials. For local runs that need
credentials, copy it to the git-ignored `config/config.local.json` and point `ROBOTSIX_CONFIG_FILE`
at that file.

```jsonc
{
  "llmio_model_level": 3,
  // "llmio_api_key": "sk-or-...",  // pragma: allowlist secret
  "server": {
    "host": "127.0.0.1",
    "port": 8000
  }
}
```

All fields are documented in [`config/config.json`](config/config.json) and
[`docs/configuration.md`](docs/configuration.md). A committed
[`config/config.schema.json`](config/config.schema.json) is CI-checked to stay in sync with the
`Settings` model.

### Model level

The LLM is configured the [`robotsix-llmio`](https://github.com/damien-robotsix/robotsix-llmio) way
— you pick a capability **level** (1–4) and llmio resolves the provider + model for it (via
`robotsix_llmio.config.create_model`). robotsix-chat never names a concrete provider or model. The
default level → provider-model mapping:

| `model_level` | provider-model identifier               | needs API key?         |
| ------------- | --------------------------------------- | ---------------------- |
| 1 (cheapest)  | `openrouter-deepseek/deepseek-v4-flash` | yes (`llmio_api_key`)  |
| 2             | `openrouter-deepseek/deepseek-v4-pro`   | yes (`llmio_api_key`)  |
| 3 (default)   | `claudeSDK-opus`                        | no (subscription auth) |
| 4 (frontier)  | `claudeSDK-claude-fable-5`              | no (subscription auth) |

- **Levels 3–4 / `claudeSDK`** — the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
  authenticates via your local `claude login` subscription, so **no API key**. Install with
  `uv sync --extra claude-sdk` and run `claude login` (needs Node.js on PATH).
- **Levels 1–2 / `openrouter`** — install with `uv sync --extra openrouter` and set `llmio_api_key`
  in your `config/config.json`.

Each backend dependency is pulled **through** robotsix-llmio's own extras
(`robotsix-llmio[claude-sdk]` / `robotsix-llmio[openrouter]`), so the stack owns those deps in one
place.

> Replies are returned as a single block (not token-streamed): llmio's Claude SDK model does not yet
> support incremental streaming through pydantic-ai.

### Authentication

The server ships no auth of its own (robotsix-standards component standard): production traffic is
authenticated once at the central-deploy gateway. If you expose the server any other way, put
authentication at your reverse proxy — never expose it directly to an untrusted network.

### Environment variables

The app consumes only one environment variable for config — the file locator:

| Variable               | Config key            | Default              | Description                   |
| ---------------------- | --------------------- | -------------------- | ----------------------------- |
| `ROBOTSIX_CONFIG_FILE` | *(file locator only)* | `config/config.json` | Path to the JSON config file. |

All other settings live in `config/config.json` — env vars are not a config channel for this app.

## Application factory

`create_app(agent, *, serve_ui=True, cors_allow_origins=None)` returns a Starlette ASGI app you can
mount in tests via `httpx.ASGITransport` or run with `uvicorn`. Any object with an
`async def stream(self, message: str) -> AsyncIterator[str]` method satisfies the `ChatAgent`
protocol.

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

## Standards

This repo follows the
[robotsix stack standards](https://github.com/damien-robotsix/robotsix-standards).

## License

MIT — see [`LICENSE`](LICENSE).
