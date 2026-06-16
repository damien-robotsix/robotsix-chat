"""Chat SSE server — ASGI application and entry point.

Exposes an LLM agent to human users via HTTP + Server-Sent Events, and serves
a self-contained browser chat UI from the same origin.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from importlib import resources
from typing import Protocol

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from robotsix_chat import PROJECT_TITLE
from robotsix_chat.config import Settings
from robotsix_chat.llm import Agent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE wire-format constants — single source of truth for tests and consumers.
# ---------------------------------------------------------------------------

SSE_CONTENT_TYPE = "text/event-stream"
SSE_TOKEN_TYPE = "token"
SSE_DONE_TYPE = "done"
SSE_ERROR_TYPE = "error"


def _sse_frame(payload: object) -> bytes:
    """Return an SSE ``data:`` frame with a JSON-serialised *payload*."""
    return f"data: {json.dumps(payload)}\n\n".encode()


class ChatAgent(Protocol):
    """Structural interface for an agent that streams LLM responses.

    Any object whose ``stream(message)`` method returns an
    ``AsyncIterator[str]`` satisfies this protocol — no subclassing
    required.  (An ``async def`` generator method naturally returns an
    async iterator, so real implementations just write ``async def
    stream(self, message: str) -> AsyncIterator[str]:`` with ``yield``.)
    """

    def stream(self, message: str) -> AsyncIterator[str]:
        """Yield tokens from the LLM in response to ``message``."""
        ...


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def health_endpoint(request: Request) -> JSONResponse:
    """Liveness probe — returns 200 ``{"status": "ok"}``."""
    return JSONResponse({"status": "ok"})


def _load_ui_html() -> str:
    """Read the bundled browser UI (``ui/index.html``) as a string."""
    raw = (resources.files("robotsix_chat") / "ui" / "index.html").read_text(
        encoding="utf-8"
    )
    return raw.replace("{{ PROJECT_TITLE }}", PROJECT_TITLE)


async def ui_endpoint(request: Request) -> HTMLResponse:
    """Serve the self-contained browser chat UI at ``GET /``."""
    return HTMLResponse(_load_ui_html())


async def chat_endpoint(
    request: Request,
) -> JSONResponse | StreamingResponse:
    """Accept a chat message and stream the agent's response as SSE."""
    agent: ChatAgent = request.app.state.agent

    # -- parse & validate JSON body ---------------------------------------
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    message = body.get("message")
    if not message or not isinstance(message, str):
        return JSONResponse(
            {"error": "missing or invalid 'message' field"}, status_code=400
        )

    # -- SSE async generator ----------------------------------------------

    async def sse_stream() -> AsyncIterator[bytes]:
        try:
            async for token in agent.stream(message):
                yield _sse_frame({"type": SSE_TOKEN_TYPE, "content": token})
            yield _sse_frame({"type": SSE_DONE_TYPE})
        except asyncio.CancelledError:
            logger.debug("SSE stream cancelled (client disconnect)")
        except Exception as exc:
            logger.exception("Agent stream error")
            yield _sse_frame({"type": SSE_ERROR_TYPE, "message": str(exc)})

    return StreamingResponse(
        sse_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Content-Type": SSE_CONTENT_TYPE},
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text."""
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Application factory & entry point
# ---------------------------------------------------------------------------


def create_app(
    agent: ChatAgent,
    *,
    serve_ui: bool = True,
    cors_allow_origins: list[str] | None = None,
) -> Starlette:
    """Return a Starlette ASGI app wired to ``agent``.

    The returned app is a fully-initialised ASGI application that can be
    mounted directly in tests via ``httpx.ASGITransport`` or passed to
    ``uvicorn.run()``.

    Args:
        agent: Object whose ``stream(message)`` yields response tokens.
        serve_ui: When ``True`` (default), serve the bundled browser chat
            UI at ``GET /`` so the UI and ``/chat`` share one origin.
        cors_allow_origins: Origins permitted to call ``/chat`` cross-origin
            (e.g. when the UI is hosted separately). ``None`` (default)
            adds no CORS headers; ``["*"]`` allows any origin.
    """
    routes = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/chat", chat_endpoint, methods=["POST"]),
    ]
    if serve_ui:
        routes.append(Route("/", ui_endpoint, methods=["GET"]))

    middleware = []
    if cors_allow_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=cors_allow_origins,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type"],
            )
        )

    app = Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={404: not_found_handler},
    )
    app.state.agent = agent
    return app


def run_server(
    agent: ChatAgent,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    serve_ui: bool = True,
    cors_allow_origins: list[str] | None = None,
) -> None:
    """Start the chat SSE server on ``host:port``.

    Blocks until the process is interrupted (uvicorn handles
    ``SIGINT`` / ``SIGTERM``).
    """
    import uvicorn

    app = create_app(agent, serve_ui=serve_ui, cors_allow_origins=cors_allow_origins)
    uvicorn.run(app, host=host, port=port)


class LLMChatAgent:
    """Adapter that wraps ``llm.Agent`` to satisfy the :class:`ChatAgent` protocol."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def stream(self, message: str) -> AsyncIterator[str]:
        async for token in self._agent.run(message):
            yield token


def create_agent_from_settings(
    instruction: str, settings: Settings | None = None
) -> LLMChatAgent:
    """Build an :class:`LLMChatAgent` wired from *settings*.

    When *settings* is ``None``, ``Settings.from_env()`` is called to
    load configuration from the environment / ``.env`` file.
    """
    if settings is None:
        settings = Settings.from_env()

    agent = Agent(
        instruction=instruction,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        graceful_errors=settings.graceful_errors,
    )
    return LLMChatAgent(agent)


def run_server_from_config(agent: ChatAgent | None = None) -> None:
    """Start the chat SSE server using ``Settings.from_env()`` for configuration.

    Reads ``SERVER_HOST``, ``SERVER_PORT``, ``LOG_LEVEL``, and LLM
    settings (``LLM_API_KEY``, ``LLM_MODEL``, ``LLM_BASE_URL``) from the
    environment (with ``.env`` support), configures Python logging, builds
    a default :class:`LLMChatAgent` when *agent* is ``None``, and then
    delegates to :func:`run_server`.
    """
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO)
    )
    if agent is None:
        agent = create_agent_from_settings(
            instruction="You are a helpful assistant.", settings=settings
        )
    run_server(
        agent,
        host=settings.server_host,
        port=settings.server_port,
        cors_allow_origins=settings.cors_allow_origins,
    )
