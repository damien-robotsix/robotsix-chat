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
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route

from robotsix_chat import PROJECT_TITLE
from robotsix_chat.board_reader import build_board_reader_tools
from robotsix_chat.calendar import build_calendar_tools
from robotsix_chat.chat.auth import BasicAuthConfig, BasicAuthMiddleware
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.chat.tasks import TaskRegistry
from robotsix_chat.component_client import build_component_tools
from robotsix_chat.config import Settings, level_needs_api_key
from robotsix_chat.diagnostics import build_diagnostics_tools
from robotsix_chat.direct_repo import build_direct_repo_tools
from robotsix_chat.knowledge import build_knowledge_tools
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.mail import build_mail_tools
from robotsix_chat.memory import build_memory
from robotsix_chat.mill import build_mill_tools
from robotsix_chat.pending_questions import build_pending_questions_tools
from robotsix_chat.pending_questions.store import PendingQuestionsStore
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.selfreview import build_recent_activity_tools
from robotsix_chat.version_check import build_version_check_tools

from .idempotency import MessageIdempotencyStore
from .routes import (
    ChatAgent,
    RunSerializer,
    chat_endpoint,
    events_endpoint,
    health_endpoint,
    history_endpoint,
    loops_list_endpoint,
    loops_stop_endpoint,
    not_found_handler,
    pending_questions_answer_endpoint,
    pending_questions_delete_endpoint,
    pending_questions_list_endpoint,
    pending_questions_thread_append_endpoint,
    pending_questions_thread_get_endpoint,
    server_error_handler,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    ui_endpoint,
)

if TYPE_CHECKING:
    from robotsix_chat.chat.loops import CheckLoopRegistry
    from robotsix_chat.chat.runner import DeliveryChannel

logger = logging.getLogger(__name__)


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


def create_app(
    agent: ChatAgent,
    *,
    serve_ui: bool = True,
    idle_timeout_minutes: int = 30,
    max_images_per_message: int = 8,
    max_image_bytes: int = 5_242_880,
    allowed_image_media_types: list[str] | None = None,
    cors_allow_origins: list[str] | None = None,
    auth: BasicAuthConfig | None = None,
    correlation_id_header: str = "X-Request-ID",
    conversation_store: ConversationStore | None = None,
    task_registry: TaskRegistry | None = None,
    event_bus: EventBus | None = None,
    check_loop_registry: CheckLoopRegistry | None = None,
    run_serializer: RunSerializer | None = None,
    msg_id_store: MessageIdempotencyStore | None = None,
    pq_store: PendingQuestionsStore | None = None,
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
        auth: When set, gate every request except ``GET /health`` behind
            HTTP Basic Auth with these credentials. ``None`` (default)
            leaves the server open.
        correlation_id_header: HTTP header name for the correlation /
            request-id. Default ``X-Request-ID``.
        conversation_store: Tracks per-client multi-turn conversation history
            and trace sessions. ``None`` (default) builds one with default
            settings (30-minute idle reset).
        task_registry: Shared registry for background sub-agent task
            lifecycle.  When ``None`` (default), a fresh registry is
            created and wired to the internal event bus.  Pass an existing
            instance to share the same registry between the foreground
            agent's delegation tool and the ``GET /events`` SSE endpoint.
        event_bus: Per-client SSE notification bus for ``GET /events``.
            When ``None`` (default), a fresh :class:`EventBus` is created.
            Pass the same instance given to the :class:`TaskRegistry` so
            lifecycle frames published by the registry reach the SSE
            subscribers.
        check_loop_registry: Shared registry for recurring check-loop
            lifecycle.  Leave ``None`` (default) when check loops are
            not wired — the stop route returns 503 and the resume hook
            is skipped.
        run_serializer: Per-owner ``RunSerializer`` that prevents
            overlapping agent runs for the same owner.  When ``None``
            (default), a fresh ``RunSerializer`` is created.  Pass the
            same instance to the ``ConversationDeliveryChannel`` so
            tick-triggered runs and user-initiated ``/chat`` requests
            are serialized together.
        msg_id_store: Per-session message idempotency store that ensures
            duplicate messages return the cached reply.  When ``None``
            (default), a fresh :class:`MessageIdempotencyStore` is created.
        pq_store: Per-server pending-questions store for the real-time
            agent-questions panel.  When ``None`` (default), a fresh
            :class:`PendingQuestionsStore` is created and wired to the
            internal event bus.  Pass an existing instance to share state.
        on_startup: Optional callable invoked during application startup
            (the Starlette lifespan ``startup`` phase).  Pass a closure
            that e.g. resumes persisted check loops.
        on_startup_async: Optional async callable invoked after *on_startup*
            during application startup.  Pass a coroutine function that
            e.g. starts the component-agent responder.
        on_shutdown: Optional async callable invoked during application
            shutdown (after ``yield``).  Pass a coroutine function that
            e.g. stops the component-agent responder.

    """
    routes = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/chat", chat_endpoint, methods=["POST"]),
        Route("/events", events_endpoint, methods=["GET"]),
        Route("/history", history_endpoint, methods=["GET"]),
        Route("/loops/{loop_id}/stop", loops_stop_endpoint, methods=["POST"]),
        Route("/loops", loops_list_endpoint, methods=["GET"]),
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
        Route("/pending-questions", pending_questions_list_endpoint, methods=["GET"]),
        Route(
            "/pending-questions/{question_id}/answer",
            pending_questions_answer_endpoint,
            methods=["POST"],
        ),
        Route(
            "/pending-questions/{question_id}",
            pending_questions_delete_endpoint,
            methods=["DELETE"],
        ),
        Route(
            "/pending-questions/{question_id}/thread",
            pending_questions_thread_append_endpoint,
            methods=["POST"],
        ),
        Route(
            "/pending-questions/{question_id}/thread",
            pending_questions_thread_get_endpoint,
            methods=["GET"],
        ),
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
        lifespan=lambda app: _make_lifespan(
            on_startup,
            on_startup_async=on_startup_async,
            on_shutdown=on_shutdown,
        ),
    )
    app.state.agent = agent
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
    app.state.task_registry = task_registry or TaskRegistry(
        event_sink=app.state.event_bus
    )
    app.state.check_loop_registry = check_loop_registry  # may be None
    app.state.run_serializer = run_serializer or RunSerializer()
    app.state.msg_id_store = msg_id_store or MessageIdempotencyStore()
    app.state.pq_store = pq_store or PendingQuestionsStore(
        event_bus=app.state.event_bus
    )
    return app


def create_agent_from_settings(
    instruction: str | None = None,
    settings: Settings | None = None,
    *,
    task_registry: TaskRegistry | None = None,
    delivery_channel: DeliveryChannel | None = None,
    check_loop_registry: CheckLoopRegistry | None = None,
    conversation_store: ConversationStore | None = None,
    model_override: str | None = None,
    tool_wrapper: Callable[[list[Any]], list[Any]] | None = None,
    pq_store: PendingQuestionsStore | None = None,
) -> LlmioChatAgent:
    """Build an :class:`LlmioChatAgent` wired from *settings*.

    The backend is chosen by robotsix-llmio's capability ``model_level``
    (``settings.llmio_model_level``) — the level encodes the transport + model.
    ``settings.llmio_api_key`` is forwarded only when that level's transport
    needs a key (so keyless levels like claude-sdk never receive one).

    *model_override* is a bare model name (e.g. ``"sonnet"``) passed through
    to :class:`LlmioChatAgent` as ``model_name=``.  When ``None`` (the
    default), the model is resolved from the level's tier default.

    When *settings* is ``None``, ``Settings.load()`` resolves configuration
    from the YAML config file and environment. When *instruction* is ``None``,
    it is taken from ``settings.agent_instruction``.

    Long-term memory is attached when ``settings.memory.enabled`` is set
    (otherwise a no-op memory is used and the agent stays stateless). The mill
    consult tool is attached when ``settings.mill.enabled`` is set; the calendar
    and task tools are attached when ``settings.calendar.enabled`` is set; the
    reference-docs tools are attached when ``settings.refdocs.enabled`` is set;
    the self-review ``read_recent_activity`` tool is attached when
    ``settings.self_review.enabled`` is set and *conversation_store* is provided
    (otherwise no tools are added).

    When both *task_registry* and *delivery_channel* are provided, the
    ``delegate_task`` tool is also added so the agent can offload long-running
    work to a background sub-agent.  Sub-agents built by the runner — which
    omits these two arguments — do **not** receive the delegation tool,
    preventing infinite recursion.

    When *check_loop_registry* is provided, the ``start_check_loop`` tool is
    added so the agent can launch a recurring check loop on the user's behalf.
    Sub-agents (which omit it) do not receive the loop tool, preventing
    recursion.
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
    tools: list[Any] = [
        *build_mill_tools(settings.mill),
        *build_mail_tools(settings.mail),
        *build_calendar_tools(settings.calendar),
        *build_component_tools(settings.component_client),
        *build_refdocs_tools(settings.refdocs),
        *build_board_reader_tools(settings.board_reader),
        *build_direct_repo_tools(settings.direct_repo),
        *build_knowledge_tools(settings.knowledge),
        *build_diagnostics_tools(settings.diagnostics),
        *build_recent_activity_tools(settings.self_review, conversation_store),
        *build_version_check_tools(settings.version_check),
    ]
    if tool_wrapper is not None:
        tools = tool_wrapper(tools)
    # Attach per-request tools from independently-gated sources so the
    # foreground agent can delegate work and launch check loops.
    # The factory lambda is called once per stream() invocation with the
    # request's session id (passed via stream()'s client_id argument), so tool
    # closures capture the owning session lexically — surviving the
    # claude_sdk/MCP boundary.  Background tasks and check loops are therefore
    # scoped to the session that spawned them.
    request_tools_factory: Callable[[str], list[Any]] | None = None
    if (
        (task_registry is not None and delivery_channel is not None)
        or check_loop_registry is not None
        or pq_store is not None
    ):
        from robotsix_chat.chat.delegation import (
            build_check_loop_tools,
            build_delegation_tools,
        )
        from robotsix_chat.chat.runner import NULL_CHANNEL

        # Compute the sub-agent model override ONCE.  The override is only
        # meaningful for the keyless claudeSDK provider (level 3); for
        # OpenRouter levels (1–2) a bare "sonnet"/"haiku" is not a valid
        # model name, so the override is suppressed.
        subagent_model = (
            settings.subagent_model
            if (
                settings.subagent_model
                and not level_needs_api_key(settings.llmio_model_level)
            )
            else None
        )
        check_loop_model = (
            settings.check_loop_model
            if (
                settings.check_loop_model
                and not level_needs_api_key(settings.llmio_model_level)
            )
            else None
        )

        def _subagent_factory(s: Settings) -> LlmioChatAgent:
            """Build a sub-agent with the downgraded model.

            When the override is suppressed the foreground model is used.
            Omits task_registry, delivery_channel, and check_loop_registry so
            sub-agents get neither delegate_task nor start_check_loop tools —
            preserving the recursion guard.
            """
            return create_agent_from_settings(settings=s, model_override=subagent_model)

        def _check_loop_factory(s: Settings) -> LlmioChatAgent:
            """Build a check-loop tick agent with the monitoring model override.

            Uses ``check_loop_model`` (default ``"haiku"``) instead of
            ``subagent_model`` so monitoring/polling ticks run on the
            cheapest subscription tier, independently of the model used
            for delegation tasks.
            """
            return create_agent_from_settings(
                settings=s, model_override=check_loop_model
            )

        def _make_request_tools(session_id: str) -> list[Any]:
            request_tools: list[Any] = []
            if task_registry is not None and delivery_channel is not None:
                request_tools.extend(
                    build_delegation_tools(
                        settings,
                        task_registry,
                        delivery_channel,
                        session_id=session_id,
                        agent_factory=_subagent_factory,
                        conversation_store=conversation_store,
                    )
                )
            if check_loop_registry is not None:
                request_tools.extend(
                    build_check_loop_tools(
                        settings,
                        check_loop_registry,
                        delivery_channel or NULL_CHANNEL,
                        session_id=session_id,
                        agent_factory=_check_loop_factory,
                        conversation_store=conversation_store,
                    )
                )
            if pq_store is not None:
                request_tools.extend(
                    build_pending_questions_tools(
                        settings.pending_questions,
                        pq_store,
                        session_id=session_id,
                    )
                )
            return request_tools

        request_tools_factory = _make_request_tools
    return LlmioChatAgent(
        model_level=settings.llmio_model_level,
        instruction=instruction,
        api_key=api_key,
        memory=build_memory(settings.memory),
        tools=tools,
        request_tools_factory=request_tools_factory,
        model_name=model_override,
    )
