# robotsix-chat

**Browser + SSE chat server for an LLM agent** — exposes an LLM agent to
human users over HTTP, with a self-contained browser chat UI.

`robotsix-chat` wraps [`llmio`](https://pypi.org/project/llmio/)'s tool-calling
loop in a small streaming `Agent`, then serves it over Server-Sent Events:

- `GET /` — a single-file browser chat UI (no build step, no framework).
- `POST /chat` — accepts `{"message": "..."}` and streams the agent's reply
  as SSE token frames.
- `GET /health` — liveness probe.

The UI and the API are served from the **same origin** by default, so the
browser talks to `/chat` with no CORS configuration. When you host the UI
separately, set `CORS_ALLOW_ORIGINS`.

> This package was split out of
> [`robotsix-agent-comm`](https://github.com/damien-robotsix/robotsix-agent-comm):
> the chat server never depended on that project's message protocol /
> transport, so it now lives on its own. `robotsix-agent-comm` is back to
> being a stdlib-only agent-to-agent communication stack.

## Quick start

```bash
git clone https://github.com/damien-robotsix/robotsix-chat.git
cd robotsix-chat
uv sync
```

### Run against a real LLM

Copy the example environment file and edit as needed:

```bash
cp .env.example .env
```

Then configure the LLM via environment variables (the `.env` file is picked up
automatically) and start the server:

```bash
export LLM_API_KEY=sk-...
export LLM_MODEL=gpt-4o-mini        # optional
# export LLM_BASE_URL=...           # optional, for OpenAI-compatible providers
uv run robotsix-chat
```

Open <http://127.0.0.1:8000/> in your browser and start chatting.

### Run without an API key (echo agent)

To exercise the UI with no LLM credentials, point the server at a trivial
echo agent. Create `demo.py`:

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

All configuration is via environment variables (with `.env` support).

| Variable | Default | Description |
|---|---|---|
| `LLM_API_KEY` | *(required)* | Provider API key. |
| `LLM_MODEL` | `gpt-4o-mini` | Model name. |
| `LLM_BASE_URL` | *(none)* | Custom base URL for OpenAI-compatible providers. |
| `SERVER_HOST` | `127.0.0.1` | Host the server binds to. |
| `SERVER_PORT` | `8000` | Port the server listens on. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `CORS_ALLOW_ORIGINS` | *(empty)* | Comma-separated origins allowed to call `/chat` cross-origin (`*` = any). Only needed when the UI is hosted elsewhere. |

## Application factory

`create_app(agent, *, serve_ui=True, cors_allow_origins=None)` returns a
Starlette ASGI app you can mount in tests via `httpx.ASGITransport` or run
with `uvicorn`. Any object with an
`async def stream(self, message: str) -> AsyncIterator[str]` method
satisfies the `ChatAgent` protocol.

## Development

```bash
uv sync
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run mypy .                    # static type checking (strict)
uv run pytest                    # tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full contributor setup, including
pre-commit hooks and dependency auditing.

## License

MIT — see [`LICENSE`](LICENSE).
