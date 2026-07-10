"""Chat SSE server — application factory and agent construction.

``create_app`` wires middlewares, routes, lifespan, and all shared state
into a Starlette ASGI application.  ``create_agent_from_settings`` builds
the LLM agent with its full tool suite from configuration.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from importlib import resources
from typing import TYPE_CHECKING, Any

from asgi_correlation_id import CorrelationIdMiddleware
from asgi_correlation_id.context import correlation_id
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from structlog.contextvars import bind_contextvars, clear_contextvars

from robotsix_chat import PROJECT_TITLE
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus, EventSink
from robotsix_chat.component_access import build_component_access_tools
from robotsix_chat.component_client import build_component_tools
from robotsix_chat.config import Settings, level_needs_api_key
from robotsix_chat.diagnostics import build_diagnostics_tools
from robotsix_chat.github import build_github_security_tools
from robotsix_chat.knowledge import build_knowledge_tools
from robotsix_chat.lifecycle import build_lifecycle_tools
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.mail import build_mail_tools
from robotsix_chat.memory import NullMemory, build_memory
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.study import build_repo_study_tools
from robotsix_chat.selfreview import build_recent_activity_tools
from robotsix_chat.version_check import build_version_check_tools

from .idempotency import MessageIdempotencyStore
from .routes import (
    ChatAgent,
    MessageCoalescer,
    RunSerializer,
    chat_endpoint,
    events_endpoint,
    health_endpoint,
    history_endpoint,
    http_exception_handler,
    not_found_handler,
    server_error_handler,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    subsessions_close_endpoint,
    subsessions_get_endpoint,
    subsessions_list_endpoint,
    subsessions_message_endpoint,
    subsessions_transcript_endpoint,
    summary_endpoint,
    ui_endpoint,
)

if TYPE_CHECKING:
    from robotsix_chat.subsessions import (
        CloseState,
        ParentDelivery,
        SubsessionContext,
        SubsessionEnv,
        SubsessionRegistry,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structlog context middleware — binds correlation_id into structlog's
# contextvars so merge_contextvars picks it up for every log line.
# ---------------------------------------------------------------------------


class StructlogContextMiddleware:
    """ASGI middleware that binds correlation_id into structlog contextvars.

    Must be placed after ``CorrelationIdMiddleware`` in the middleware
    stack so the correlation ID is already populated when this middleware
    runs.
    """

    def __init__(self, app: Any) -> None:
        """Store the downstream ASGI application."""
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Clear contextvars, bind the current correlation_id, then delegate."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        clear_contextvars()
        bind_contextvars(correlation_id=correlation_id.get())
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# UI HTML loader — reads the bundled browser UI and fills placeholders
# ---------------------------------------------------------------------------


def _load_ui_html(idle_timeout_minutes: int) -> str:
    """Read the bundled browser UI (``ui/index.html``) and fill placeholders."""
    raw = (resources.files("robotsix_chat") / "ui" / "index.html").read_text(
        encoding="utf-8"
    )
    return raw.replace("{{ PROJECT_TITLE }}", PROJECT_TITLE).replace(
        "{{ IDLE_TIMEOUT_MINUTES }}", str(idle_timeout_minutes)
    )


# ---------------------------------------------------------------------------
# Application factory & entry point
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _make_lifespan(
    on_startup: Callable[[], None] | None,
    *,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> AsyncIterator[None]:
    """Starlette lifespan that invokes hooks on startup and shutdown.

    A resume failure is logged but does not crash app startup.
    """
    if on_startup is not None:
        try:
            on_startup()
        except Exception:
            logger.exception("Startup hook failed — continuing")
    if on_startup_async is not None:
        try:
            await on_startup_async()
        except Exception:
            logger.exception("Async startup hook failed — continuing")
    try:
        yield
    finally:
        if on_shutdown is not None:
            try:
                await on_shutdown()
            except Exception:
                logger.exception("Shutdown hook failed")


# Shared keyword parameters between create_app() and run_server().
# When adding a new parameter that both functions should accept, include
# its name here — the test suite enforces parity via inspect.signature.
SHARED_PARAMS: frozenset[str] = frozenset(
    {
        "summary_agent",
        "serve_ui",
        "idle_timeout_minutes",
        "max_images_per_message",
        "max_image_bytes",
        "allowed_image_media_types",
        "cors_allow_origins",
        "correlation_id_header",
        "conversation_store",
        "event_bus",
        "run_serializer",
        "subsession_registry",
        "subsession_delivery",
        "on_startup",
        "on_startup_async",
        "on_shutdown",
    }
)


def create_app(
    agent: ChatAgent,
    *,
    summary_agent: ChatAgent | None = None,
    serve_ui: bool = True,
    idle_timeout_minutes: int = 30,
    max_images_per_message: int = 8,
    max_image_bytes: int = 5_242_880,
    allowed_image_media_types: list[str] | None = None,
    cors_allow_origins: list[str] | None = None,
    correlation_id_header: str = "X-Request-ID",
    conversation_store: ConversationStore | None = None,
    event_bus: EventBus | None = None,
    run_serializer: RunSerializer | None = None,
    msg_id_store: MessageIdempotencyStore | None = None,
    message_coalescer: MessageCoalescer | None = None,
    message_coalesce_seconds: float = 0.3,
    subsession_registry: SubsessionRegistry | None = None,
    subsession_delivery: ParentDelivery | None = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> Starlette:
    """Return a Starlette ASGI app wired to ``agent``.

    The returned app is a fully-initialised ASGI application that can be
    mounted directly in tests via ``httpx.ASGITransport`` or passed to
    ``uvicorn.run()``.

    Args:
        agent: Object whose ``stream(message)`` yields response tokens.
        summary_agent: Agent used by ``POST /summary`` to generate the
            structured conversation summary. ``None`` (default) reuses
            *agent* — pass a separate, cheaper agent (see
            ``settings.summary_model_level``) to avoid running the
            (often pricier) main agent on every turn just for extraction.
        serve_ui: When ``True`` (default), serve the bundled browser chat
            UI at ``GET /`` so the UI and ``/chat`` share one origin.
        idle_timeout_minutes: Minutes of no user activity before the UI
            auto-restarts the conversation; ``0`` disables.
        max_images_per_message: Maximum number of images a client may attach
            to a single ``POST /chat`` request.  Default ``8``.
        max_image_bytes: Maximum decoded size (bytes) of a single attached
            image.  Default ``5_242_880`` (5 MiB).
        allowed_image_media_types: Media types accepted for image
            attachments.  Default ``["image/png", "image/jpeg", "image/gif",
            "image/webp"]``.
        cors_allow_origins: Origins permitted to call ``/chat`` cross-origin
            (e.g. when the UI is hosted separately). ``None`` (default)
            adds no CORS headers; ``["*"]`` allows any origin.
        correlation_id_header: HTTP header name for the correlation /
            request-id. Default ``X-Request-ID``.
        conversation_store: Tracks per-client multi-turn conversation history
            and trace sessions. ``None`` (default) builds one with default
            settings.
        event_bus: Per-client SSE notification bus for ``GET /events``.
            When ``None`` (default), a fresh :class:`EventBus` is created.
            Pass the same instance given to the ``SubsessionRegistry`` so
            lifecycle frames published by the registry reach the SSE
            subscribers.
        run_serializer: Per-owner ``RunSerializer`` that prevents
            overlapping agent runs for the same owner.  When ``None``
            (default), a fresh ``RunSerializer`` is created.  Pass the
            same instance to the ``ParentDelivery`` so subsession summary
            writes and user-initiated ``/chat`` requests are serialized.
        msg_id_store: Per-session message idempotency store that ensures
            duplicate messages return the cached reply.  When ``None``
            (default), a fresh :class:`MessageIdempotencyStore` is created.
        message_coalescer: Per-session message coalescer that batches
            rapid-fire user messages into a single agent run.  When
            ``None`` (default), a fresh :class:`MessageCoalescer` is
            created with *message_coalesce_seconds*.
        message_coalesce_seconds: Debounce window (seconds) that the
            coalescer waits before draining the pending-message batch.
            Default ``0.3``.
        subsession_registry: Shared
            :class:`~robotsix_chat.subsessions.SubsessionRegistry` for the
            unified subsession system.  Leave ``None`` when subsessions are
            not wired — the ``/subsessions`` routes then return 503.
        subsession_delivery: The
            :class:`~robotsix_chat.subsessions.ParentDelivery` used by the
            ``/subsessions/{id}/close`` route to deliver the summary of an
            externally-closed subsession.  Required together with
            *subsession_registry* for full functionality.
        on_startup: Optional callable invoked during application startup
            (the Starlette lifespan ``startup`` phase).  Pass a closure
            that e.g. resumes persisted subsessions.
        on_startup_async: Optional async callable invoked after *on_startup*
            during application startup.  Pass a coroutine function that
            e.g. starts the component-agent responder.
        on_shutdown: Optional async callable invoked during application
            shutdown (after ``yield``).  Pass a coroutine function that
            e.g. stops the component-agent responder.

    """
    routes: list[Route | Mount] = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/chat", chat_endpoint, methods=["POST"]),
        Route("/events", events_endpoint, methods=["GET"]),
        Route("/history", history_endpoint, methods=["GET"]),
        Route("/summary", summary_endpoint, methods=["POST"]),
        Route("/sessions", sessions_list_endpoint, methods=["GET"]),
        Route("/sessions", sessions_create_endpoint, methods=["POST"]),
        Route(
            "/sessions/{session_id}",
            sessions_delete_endpoint,
            methods=["DELETE"],
        ),
        Route(
            "/sessions/{session_id}/close",
            sessions_close_endpoint,
            methods=["POST"],
        ),
        Route("/subsessions", subsessions_list_endpoint, methods=["GET"]),
        Route("/subsessions/{sub_id}", subsessions_get_endpoint, methods=["GET"]),
        Route(
            "/subsessions/{sub_id}/transcript",
            subsessions_transcript_endpoint,
            methods=["GET"],
        ),
        Route(
            "/subsessions/{sub_id}/message",
            subsessions_message_endpoint,
            methods=["POST"],
        ),
        Route(
            "/subsessions/{sub_id}/close",
            subsessions_close_endpoint,
            methods=["POST"],
        ),
    ]
    if serve_ui:
        routes.append(Route("/", ui_endpoint, methods=["GET"]))
        routes.append(
            Mount(
                "/static",
                app=StaticFiles(packages=["robotsix_chat"], directory="ui/static"),
            )
        )

    # CorrelationIdMiddleware is outermost so every request (and its log lines)
    # carries a request id. Authentication is centralized at the
    # central-deploy gateway — the app adds no auth layer of its own.
    middleware = [
        Middleware(CorrelationIdMiddleware, header_name=correlation_id_header),
        Middleware(StructlogContextMiddleware),
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

    app = Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={
            HTTPException: http_exception_handler,
            404: not_found_handler,
            500: server_error_handler,
        },
        lifespan=lambda _app: _make_lifespan(
            on_startup,
            on_startup_async=on_startup_async,
            on_shutdown=on_shutdown,
        ),
    )
    app.state.agent = agent
    app.state.summary_agent = summary_agent if summary_agent is not None else agent
    app.state.conversation_store = conversation_store or ConversationStore()
    app.state.idle_timeout_minutes = idle_timeout_minutes
    app.state.max_images_per_message = max_images_per_message
    app.state.max_image_bytes = max_image_bytes
    app.state.allowed_image_media_types = (
        allowed_image_media_types
        if allowed_image_media_types is not None
        else ["image/png", "image/jpeg", "image/gif", "image/webp"]
    )
    app.state.event_bus = event_bus or EventBus()
    app.state.run_serializer = run_serializer or RunSerializer()
    app.state.msg_id_store = msg_id_store or MessageIdempotencyStore()
    app.state.message_coalescer = message_coalescer or MessageCoalescer(
        debounce_seconds=message_coalesce_seconds
    )
    app.state.subsession_registry = subsession_registry  # may be None
    app.state.subsession_delivery = subsession_delivery  # may be None
    return app


def create_agent_from_settings(
    instruction: str | None = None,
    settings: Settings | None = None,
    *,
    conversation_store: ConversationStore | None = None,
    model_level: int | None = None,
    subsession_env: SubsessionEnv | None = None,
    subsession_ctx: SubsessionContext | None = None,
    subsession_close_state: CloseState | None = None,
    tool_wrapper: Callable[[list[Any]], list[Any]] | None = None,
    bare: bool = False,
    event_sink: EventSink | None = None,
) -> LlmioChatAgent:
    """Build an :class:`LlmioChatAgent` wired from *settings*.

    The backend is chosen by robotsix-llmio's capability level: *model_level*
    when given, else ``settings.llmio_model_level``.  The level encodes the
    transport + model; ``settings.llmio_api_key`` is forwarded only when the
    effective level's transport needs a key (keyless claudeSDK levels 3-4
    never receive one).

    When *settings* is ``None``, ``Settings.load()`` resolves configuration
    from the YAML config file and environment. When *instruction* is ``None``,
    it is taken from ``settings.agent_instruction``.

    Long-term memory is attached when ``settings.memory.enabled`` is set;
    every feature tool suite (mail, board reader, …) is
    attached according to its own settings gate — for the main agent and
    subsession agents alike.

    *bare* (default ``False``) skips all of that: no component-access/
    roster/lifecycle instruction augmentation, no feature tools, no
    subsession wiring, and memory is a no-op ``NullMemory`` instead of
    ``build_memory(settings.memory)``. Use it for a single bounded
    text-transformation call (e.g. the ``POST /summary`` agent) that has
    no business paying for cross-session memory recall or agentic tool
    access — ``ChatMemory.recall()`` alone was observed taking 90+
    seconds in production, dwarfing the actual (cheap-tier) model call.

    Subsession wiring (*subsession_env*):

    * **Main chat agent** — pass *subsession_env* with ``subsession_ctx=None``.
      The subsession tools (spawn/message/close/list) are built **per
      request** via ``request_tools_factory``, so each tool closure captures
      the owning ``session_id`` lexically (surviving the claude_sdk/MCP
      boundary).
    * **Subsession agent** — pass *subsession_env*, the worker's
      *subsession_ctx*, and its *subsession_close_state*.  The depth-aware
      tools (including ``complete_subsession``) are baked in statically;
      identity is fixed at construction.
    * ``None`` (default) — no subsession tools (bare agent, e.g. tests).

    *event_sink* is forwarded to :class:`~robotsix_chat.llm.LlmioChatAgent`
    so a claudeSDK turn's live tool/thinking activity is published as
    ``activity`` frames on the ``GET /events`` channel. Pass the same
    :class:`~robotsix_chat.chat.events.EventBus` given to ``create_app`` —
    typically only for the main chat agent, not the bare ``/summary`` agent.
    """
    if settings is None:
        settings = Settings.load()
    if instruction is None:
        instruction = settings.agent_instruction

    # Inject component-access instruction and skill prompts from the
    # central-deploy roster — only when a roster URL is configured.
    if not bare and settings.central_deploy.url:
        instruction = (
            f"{instruction}\n\n"
            "Component access:\n"
            "– You have one generic tool for calling external components: "
            "component_request(component_id, method, path, json_body=None). "
            "Each component declares its own API surface as a skill — read "
            "the skill descriptions below for allowed operations.\n"
            "– Obey each component skill's safety section. When a skill marks "
            "an operation as requiring confirmation, ask the user in "
            "conversation before calling it.\n"
            "– If the roster is unavailable or a component returns an error, "
            "report the error clearly — do not retry in a loop."
        )
        from robotsix_chat.component_access.roster import (
            build_skill_prompt,
            fetch_roster_sync,
        )

        roster = fetch_roster_sync(settings.central_deploy)
        skill_prompt = build_skill_prompt(roster)
        if skill_prompt:
            instruction = f"{instruction}\n\n{skill_prompt}"

    # Inject the lifecycle component skill when lifecycle is enabled.
    if not bare and settings.lifecycle.enabled:
        from robotsix_chat.lifecycle import load_lifecycle_skill

        lifecycle_skill = load_lifecycle_skill()
        if lifecycle_skill:
            instruction = f"{instruction}\n\n{lifecycle_skill}"

    effective_level = (
        model_level if model_level is not None else settings.llmio_model_level
    )
    api_key = (
        settings.llmio_api_key.get_secret_value()
        if level_needs_api_key(effective_level)
        else ""
    )
    tools: list[Any] = (
        []
        if bare
        else [
            *build_component_access_tools(settings.central_deploy),
            *build_mail_tools(settings.mail),
            *build_component_tools(settings.component_client),
            *build_refdocs_tools(settings.refdocs),
            *build_repo_study_tools(settings.repo_study, settings.direct_repo),
            *build_direct_repo_tools(settings.direct_repo),
            *build_github_security_tools(
                settings.github_security, settings.direct_repo
            ),
            *build_knowledge_tools(settings.knowledge),
            *build_diagnostics_tools(settings.diagnostics),
            *build_recent_activity_tools(settings.self_review, conversation_store),
            *build_version_check_tools(settings.version_check),
            *build_lifecycle_tools(settings.lifecycle),
        ]
    )
    if tool_wrapper is not None:
        tools = tool_wrapper(tools)

    request_tools_factory: Callable[[str], list[Any]] | None = None
    if not bare and subsession_env is not None:
        from robotsix_chat.subsessions import (
            SubsessionContext as _Ctx,
        )
        from robotsix_chat.subsessions import (
            build_subsession_tools,
        )

        if subsession_ctx is not None:
            # Subsession agent: identity fixed at construction.
            tools.extend(
                build_subsession_tools(
                    subsession_env,
                    ctx=subsession_ctx,
                    close_state=subsession_close_state,
                )
            )
        else:
            # Main chat agent: build the tools once per stream() call with
            # the request's session id (passed via stream()'s client_id) so
            # closures capture the owning session lexically.
            env = subsession_env

            def _make_request_tools(session_id: str) -> list[Any]:
                return build_subsession_tools(
                    env,
                    ctx=_Ctx(
                        owner_session_id=session_id,
                        subsession_id=None,
                        depth=0,
                    ),
                )

            request_tools_factory = _make_request_tools

    return LlmioChatAgent(
        model_level=effective_level,
        instruction=instruction,
        api_key=api_key,
        memory=NullMemory() if bare else build_memory(settings.memory),
        tools=tools,
        request_tools_factory=request_tools_factory,
        event_sink=event_sink,
    )
