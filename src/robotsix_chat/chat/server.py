"""Chat SSE server — ASGI application and entry point.

Exposes an LLM agent to human users via HTTP + Server-Sent Events, and serves
a self-contained browser chat UI from the same origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.config
from collections.abc import AsyncIterator
from importlib import resources
from typing import Protocol

from asgi_correlation_id import CorrelationIdMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from robotsix_chat import PROJECT_TITLE
from robotsix_chat.chat.auth import BasicAuthConfig, BasicAuthMiddleware
from robotsix_chat.config import Settings, level_needs_api_key
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.memory import build_memory
from robotsix_chat.mill import build_mill_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE wire-format constants — single source of truth for tests and consumers.
# ---------------------------------------------------------------------------

SSE_CONTENT_TYPE = "text/event-stream"
SSE_TOKEN_TYPE = "token"
SSE_DONE_TYPE = "done"
SSE_ERROR_TYPE = "error"

# The agent returns its reply as a single block only once the whole pipeline
# (memory recall, the LLM, any tool calls) completes — which can be many seconds
# with no bytes on the wire. A silent connection that long gets dropped by the
# browser/proxy ("NetworkError"). Emit an SSE *comment* heartbeat immediately and
# on this interval so the data channel never goes quiet. Comments carry no
# ``data:`` line, so clients ignore them.
SSE_HEARTBEAT_INTERVAL = 5.0
_SSE_HEARTBEAT_FRAME = b": keepalive\n\n"


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


def _load_ui_html(idle_timeout_minutes: float) -> str:
    """Read the bundled browser UI (``ui/index.html``) and fill placeholders."""
    raw = (resources.files("robotsix_chat") / "ui" / "index.html").read_text(
        encoding="utf-8"
    )
    return raw.replace("{{ PROJECT_TITLE }}", PROJECT_TITLE).replace(
        "{{ IDLE_TIMEOUT_MINUTES }}", str(idle_timeout_minutes)
    )


async def ui_endpoint(request: Request) -> HTMLResponse:
    """Serve the self-contained browser chat UI at ``GET /``."""
    idle_timeout_minutes: float = request.app.state.idle_timeout_minutes
    return HTMLResponse(_load_ui_html(idle_timeout_minutes))


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
        # Drive the agent in a background task and forward its output through a
        # queue, so the response loop can interleave heartbeats while the agent
        # works (it yields its reply only at the end, after a long quiet spell).
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def _produce() -> None:
            try:
                async for token in agent.stream(message):
                    await queue.put((SSE_TOKEN_TYPE, token))
                await queue.put((SSE_DONE_TYPE, None))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Agent stream error")
                await queue.put((SSE_ERROR_TYPE, str(exc)))

        producer = asyncio.create_task(_produce())
        try:
            yield _SSE_HEARTBEAT_FRAME  # first byte immediately
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), SSE_HEARTBEAT_INTERVAL
                    )
                except TimeoutError:
                    yield _SSE_HEARTBEAT_FRAME
                    continue
                if kind == SSE_TOKEN_TYPE:
                    yield _sse_frame({"type": SSE_TOKEN_TYPE, "content": payload})
                elif kind == SSE_DONE_TYPE:
                    yield _sse_frame({"type": SSE_DONE_TYPE})
                    break
                else:  # SSE_ERROR_TYPE
                    yield _sse_frame({"type": SSE_ERROR_TYPE, "message": payload})
                    break
        except asyncio.CancelledError:
            logger.debug("SSE stream cancelled (client disconnect)")
            raise
        finally:
            producer.cancel()
            with contextlib.suppress(BaseException):
                await producer

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


async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unhandled server errors."""
    return JSONResponse({"error": "internal server error"}, status_code=500)


# ---------------------------------------------------------------------------
# Application factory & entry point
# ---------------------------------------------------------------------------


def create_app(
    agent: ChatAgent,
    *,
    serve_ui: bool = True,
    cors_allow_origins: list[str] | None = None,
    auth: BasicAuthConfig | None = None,
    correlation_id_header: str = "X-Request-ID",
    idle_timeout_minutes: float = 10.0,
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
        auth: When set, gate every request except ``GET /health`` behind
            HTTP Basic Auth with these credentials. ``None`` (default)
            leaves the server open.
        correlation_id_header: HTTP header name for the correlation /
            request-id. Default ``X-Request-ID``.
        idle_timeout_minutes: Minutes of browser inactivity before the UI
            auto-starts a new conversation. Set to ``0`` to disable.
            Default ``10``.

    """
    routes = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/chat", chat_endpoint, methods=["POST"]),
    ]
    if serve_ui:
        routes.append(Route("/", ui_endpoint, methods=["GET"]))

    # CorrelationIdMiddleware is outermost so every request (and its log lines)
    # carries a request id. CORS comes next so it can answer preflight
    # ``OPTIONS`` (which carry no credentials) before the auth layer rejects them.
    middleware = [
        Middleware(CorrelationIdMiddleware, header_name=correlation_id_header)
    ]
    if cors_allow_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=cors_allow_origins,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type"],
            )
        )
    if auth is not None:
        middleware.append(Middleware(BasicAuthMiddleware, config=auth))

    app = Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={
            404: not_found_handler,
            500: server_error_handler,
        },
    )
    app.state.agent = agent
    app.state.idle_timeout_minutes = idle_timeout_minutes
    return app


def run_server(
    agent: ChatAgent,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    serve_ui: bool = True,
    cors_allow_origins: list[str] | None = None,
    auth: BasicAuthConfig | None = None,
    correlation_id_header: str = "X-Request-ID",
    idle_timeout_minutes: float = 10.0,
) -> None:
    """Start the chat SSE server on ``host:port``.

    Blocks until the process is interrupted (uvicorn handles
    ``SIGINT`` / ``SIGTERM``).
    """
    import uvicorn

    app = create_app(
        agent,
        serve_ui=serve_ui,
        cors_allow_origins=cors_allow_origins,
        auth=auth,
        correlation_id_header=correlation_id_header,
        idle_timeout_minutes=idle_timeout_minutes,
    )
    uvicorn.run(app, host=host, port=port)


def create_agent_from_settings(
    instruction: str | None = None, settings: Settings | None = None
) -> LlmioChatAgent:
    """Build an :class:`LlmioChatAgent` wired from *settings*.

    The backend is chosen by robotsix-llmio's capability ``model_level``
    (``settings.llmio_model_level``) — the level encodes the transport + model.
    ``settings.llmio_api_key`` is forwarded only when that level's transport
    needs a key (so keyless levels like claude-sdk never receive one).

    When *settings* is ``None``, ``Settings.load()`` resolves configuration
    from the YAML config file and environment. When *instruction* is ``None``,
    it is taken from ``settings.agent_instruction``.

    Long-term memory is attached when ``settings.memory.enabled`` is set
    (otherwise a no-op memory is used and the agent stays stateless). The mill
    consult tool is attached when ``settings.mill.enabled`` is set (otherwise no
    tools are added).
    """
    if settings is None:
        settings = Settings.load()
    if instruction is None:
        instruction = settings.agent_instruction

    api_key = (
        settings.llmio_api_key
        if level_needs_api_key(settings.llmio_model_level)
        else ""
    )
    return LlmioChatAgent(
        model_level=settings.llmio_model_level,
        instruction=instruction,
        api_key=api_key,
        memory=build_memory(settings.memory),
        tools=build_mill_tools(settings.mill),
    )


def _setup_observability() -> None:
    """Configure Langfuse tracing and OTel-aware logging (idempotent).

    Both calls are no-ops when their prerequisites are absent:
    * ``setup_langfuse_tracing`` returns ``False`` when ``LANGFUSE_PUBLIC_KEY``
      / ``LANGFUSE_SECRET_KEY`` env vars are unset.
    * ``setup_logging`` is always safe to call; it only configures the
      ``robotsix_llmio`` logger namespace and leaves the root logger alone.

    Both are wrapped in a blanket ``ImportError`` guard so the server still
    starts when the ``tracing`` optional-dependency extra is not installed.
    """
    try:
        from robotsix_llmio.core.tracing import setup_langfuse_tracing
        from robotsix_llmio.logging import setup_logging
    except ImportError:
        logger.debug("robotsix-llmio tracing extras not installed — skipping")
        return

    setup_logging()
    setup_langfuse_tracing()


def run_server_from_config(agent: ChatAgent | None = None) -> None:
    """Start the chat SSE server using ``Settings.load()`` for configuration.

    Resolves settings through the full cascade (pydantic defaults → YAML
    config file → environment, with ``.env`` support), configures Python
    logging, builds a default :class:`LlmioChatAgent` when *agent* is
    ``None`` (using ``agent_instruction`` from config), enables HTTP Basic
    Auth when ``auth.enabled`` is set, and delegates to :func:`run_server`.
    """
    settings = Settings.load()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "correlation_id": {
                    "()": "asgi_correlation_id.CorrelationIdFilter",
                }
            },
            "formatters": {
                "default": {
                    "format": (
                        "%(asctime)s %(levelname)-8s "
                        "[%(correlation_id)s] %(name)s %(message)s"
                    ),
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["correlation_id"],
                },
            },
            "root": {
                "level": settings.log_level.upper(),
                "handlers": ["default"],
            },
        }
    )

    # -- tracing / observability (graceful no-op when deps or creds absent) --
    _setup_observability()

    if agent is None:
        agent = create_agent_from_settings(settings=settings)

    auth = (
        BasicAuthConfig(
            username=settings.auth.username, password=settings.auth.password
        )
        if settings.auth.enabled
        else None
    )
    run_server(
        agent,
        host=settings.server_host,
        port=settings.server_port,
        cors_allow_origins=settings.cors_allow_origins,
        auth=auth,
        correlation_id_header=settings.correlation_id_header,
        idle_timeout_minutes=settings.idle_timeout_minutes,
    )
