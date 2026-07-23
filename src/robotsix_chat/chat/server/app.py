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
from robotsix_chat.http_probe import build_http_probe_tools
from robotsix_chat.knowledge import build_knowledge_tools
from robotsix_chat.lifecycle import build_lifecycle_tools
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.mail import build_mail_tools
from robotsix_chat.memory import NullMemory, build_memory
from robotsix_chat.notification import build_notification_tools
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.render_url import build_render_url_tools
from robotsix_chat.repo.actions import build_github_actions_tools
from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.security import build_github_security_tools
from robotsix_chat.repo.study import build_repo_study_tools
from robotsix_chat.selfreview import build_recent_activity_tools
from robotsix_chat.version_check import build_version_check_tools

from .idempotency import MessageIdempotencyStore
from .routes import (
    ChatAgent,
    MessageCoalescer,
    RunSerializer,
    cancel_queued_endpoint,
    chat_endpoint,
    config_get_endpoint,
    config_save_endpoint,
    draft_get_endpoint,
    draft_save_endpoint,
    events_endpoint,
    github_actions_secret_endpoint,
    github_actions_workflow_endpoint,
    github_create_repo_endpoint,
    github_settings_endpoint,
    health_endpoint,
    history_endpoint,
    http_exception_handler,
    not_found_handler,
    server_error_handler,
    sessions_approve_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    sessions_reject_endpoint,
    subsessions_close_endpoint,
    subsessions_get_endpoint,
    subsessions_list_endpoint,
    subsessions_message_endpoint,
    subsessions_transcript_endpoint,
    summary_endpoint,
    ui_endpoint,
    unhandled_exception_handler,
)

if TYPE_CHECKING:
    from robotsix_chat.autonomous import AutonomousRunner
    from robotsix_chat.config.models import (
        DirectRepoSettings,
        GitHubActionsSettings,
        GitHubSecuritySettings,
    )
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
        "compaction_min_turns",
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
        "feedback_runner",
        "autonomous_runner",
        "on_startup",
        "on_startup_async",
        "on_shutdown",
        "direct_repo_settings",
        "github_security_settings",
        "github_actions_settings",
        "config_path",
    }
)


def create_app(
    agent: ChatAgent,
    *,
    summary_agent: ChatAgent | None = None,
    serve_ui: bool = True,
    idle_timeout_minutes: int = 30,
    compaction_min_turns: int = 3,
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
    feedback_runner: Any = None,
    autonomous_runner: AutonomousRunner | None = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
    direct_repo_settings: DirectRepoSettings | None = None,
    github_security_settings: GitHubSecuritySettings | None = None,
    github_actions_settings: GitHubActionsSettings | None = None,
    config_path: str | None = None,
    draft_store_dir: str | None = None,
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
        compaction_min_turns: Minimum fresh (not yet summarized) turns a
            conversation needs before an idle timeout triggers in-place
            compaction; below this the summary agent is not invoked.
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
        feedback_runner: Optional
            :class:`~robotsix_chat.feedback.FeedbackRunner` that schedules
            feedback analysis runs at compaction and session-end boundaries.
            When ``None`` (default), feedback runs are disabled.
        autonomous_runner: Optional
            :class:`~robotsix_chat.autonomous.AutonomousRunner` that drives
            the autonomous-session state machine and auto-continue loops.
            When ``None`` (default), autonomous sessions are disabled.
        on_startup: Optional callable invoked during application startup
            (the Starlette lifespan ``startup`` phase).  Pass a closure
            that e.g. resumes persisted subsessions.
        on_startup_async: Optional async callable invoked after *on_startup*
            during application startup.  Pass a coroutine function that
            e.g. starts the component-agent responder.
        on_shutdown: Optional async callable invoked during application
            shutdown (after ``yield``).  Pass a coroutine function that
            e.g. stops the component-agent responder.
        direct_repo_settings: GitHub App credentials (app id, private key,
            installation id) used by the
            ``PATCH /chat/github/repos/{owner}/{repo}/settings`` endpoint.
            When ``None``, the endpoint returns 503.
        github_security_settings: GitHub security-feature toggle config
            (org, deploy API key) used by the
            ``PATCH /chat/github/repos/{owner}/{repo}/settings`` endpoint.
            When ``None``, the endpoint returns 503.
        github_actions_settings: GitHub Actions config (org, deploy API key)
            used by the Actions secrets and workflow dispatch endpoints.
            When ``None``, the endpoints return 503.
        config_path: Path to the config JSON file, used by the
            ``GET /config`` and ``PUT /config`` endpoints.  When ``None``
            (default), the path is resolved from the
            ``ROBOTSIX_CONFIG_FILE`` environment variable or the default
            ``config/config.json``.
        draft_store_dir: Path to the session-drafts directory, used by
            the ``GET /sessions/{session_id}/draft`` and
            ``PUT /sessions/{session_id}/draft`` endpoints.  Each session
            gets its own ``{session_id}.json`` file inside this directory.
            When ``None`` (default), the directory ``/data/session_drafts``
            is used.

    """
    routes: list[Route | Mount] = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/chat", chat_endpoint, methods=["POST"]),
        Route("/chat/queue/cancel", cancel_queued_endpoint, methods=["POST"]),
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
        Route(
            "/sessions/{session_id}/approve",
            sessions_approve_endpoint,
            methods=["POST"],
        ),
        Route(
            "/sessions/{session_id}/reject",
            sessions_reject_endpoint,
            methods=["POST"],
        ),
        Route(
            "/sessions/{session_id}/draft",
            draft_get_endpoint,
            methods=["GET"],
        ),
        Route(
            "/sessions/{session_id}/draft",
            draft_save_endpoint,
            methods=["PUT"],
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
        Route(
            "/chat/github/repos/{owner}/{repo}/settings",
            github_settings_endpoint,
            methods=["PATCH"],
        ),
        Route(
            "/chat/github/repos",
            github_create_repo_endpoint,
            methods=["POST"],
        ),
        Route(
            "/chat/github/repos/{owner}/{repo}/actions/secrets/{secret_name}",
            github_actions_secret_endpoint,
            methods=["PUT"],
        ),
        Route(
            "/chat/github/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            github_actions_workflow_endpoint,
            methods=["POST"],
        ),
        Route("/config", config_get_endpoint, methods=["GET"]),
        Route("/config", config_save_endpoint, methods=["PUT"]),
    ]
    if serve_ui:
        routes.append(Route("/", ui_endpoint, methods=["GET"]))
        static_dir = str(resources.files("robotsix_chat") / "ui" / "static")
        routes.append(
            Mount(
                "/static",
                app=StaticFiles(directory=static_dir),
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
            Exception: unhandled_exception_handler,
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
    app.state.compaction_min_turns = compaction_min_turns
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
    app.state.direct_repo_settings = direct_repo_settings
    app.state.github_security_settings = github_security_settings
    app.state.github_actions_settings = github_actions_settings
    app.state.feedback_runner = feedback_runner  # may be None
    app.state.autonomous_runner = autonomous_runner  # may be None
    if config_path is not None:
        app.state.config_path = config_path
    if draft_store_dir is not None:
        app.state.draft_store_dir = draft_store_dir
    return app


def _inject_skills(
    settings: Settings,
    instruction: str,
    *,
    bare: bool = False,
) -> str:
    """Augment *instruction* with component-access instructions and skill prompts.

    Only active when *bare* is ``False``.  Each skill gate is independently
    gated by its own settings key (``central_deploy.url``,
    ``lifecycle.enabled``, ``notification.enabled``,
    ``github_security.enabled``).
    """
    if bare:
        return instruction

    # Central-deploy roster — component-access instruction + skill prompts.
    if settings.central_deploy.url:
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

    # Lifecycle skill.
    if settings.lifecycle.enabled:
        from robotsix_chat.lifecycle import load_lifecycle_skill

        lifecycle_skill = load_lifecycle_skill()
        if lifecycle_skill:
            instruction = f"{instruction}\n\n{lifecycle_skill}"

    # Notification skill.
    if settings.notification.enabled:
        from robotsix_chat.notification import load_notification_skill

        notification_skill = load_notification_skill()
        if notification_skill:
            instruction = f"{instruction}\n\n{notification_skill}"

    # HTTP probe skill.
    if settings.http_probe.enabled:
        from robotsix_chat.http_probe import load_http_probe_skill

        http_probe_skill = load_http_probe_skill()
        if http_probe_skill:
            instruction = f"{instruction}\n\n{http_probe_skill}"

    # GitHub skill.
    if settings.github_security.enabled:
        from robotsix_chat.repo.security import load_github_skill

        github_skill = load_github_skill()
        if github_skill:
            instruction = f"{instruction}\n\n{github_skill}"

    # GitHub Actions skill.
    if settings.github_actions.enabled:
        from robotsix_chat.repo.actions import load_github_actions_skill

        github_actions_skill = load_github_actions_skill()
        if github_actions_skill:
            instruction = f"{instruction}\n\n{github_actions_skill}"

    return instruction


def _build_static_tools(
    settings: Settings,
    *,
    bare: bool = False,
    conversation_store: ConversationStore | None = None,
) -> list[Any]:
    """Return the static (non-per-request) tool suite gated by *settings*.

    When *bare* is ``True`` returns an empty list — the agent gets no tools.
    """
    if bare:
        return []

    return [
        *build_component_access_tools(settings.central_deploy),
        *build_mail_tools(settings.mail),
        *build_component_tools(settings.component_client),
        *build_refdocs_tools(settings.refdocs, settings.direct_repo),
        *build_repo_study_tools(settings.repo_study, settings.direct_repo),
        *build_direct_repo_tools(settings.direct_repo),
        *build_github_security_tools(settings.github_security, settings.direct_repo),
        *build_github_actions_tools(settings.github_actions, settings.direct_repo),
        *build_knowledge_tools(settings.knowledge),
        *build_diagnostics_tools(settings.diagnostics),
        *build_recent_activity_tools(settings.self_review, conversation_store),
        *build_version_check_tools(settings.version_check, settings.direct_repo),
        *build_lifecycle_tools(settings.lifecycle),
        *build_render_url_tools(settings.render_url),
        *build_http_probe_tools(settings.http_probe),
    ]


def _build_request_tools_factory(
    settings: Settings,
    subsession_env: SubsessionEnv | None,
    event_sink: EventSink | None,
) -> Callable[[str], list[Any]] | None:
    """Build a per-request tools factory for the main chat agent.

    Combines subsession tools (built per ``stream()`` call so closures
    capture the request's session id) and, when enabled, notification
    tools.  Returns ``None`` when no per-request tools are configured.
    """
    req_factories: list[Callable[[str], list[Any]]] = []

    if subsession_env is not None:
        from robotsix_chat.subsessions import SubsessionContext as _Ctx
        from robotsix_chat.subsessions import build_subsession_tools

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

        req_factories.append(_make_request_tools)

    if settings.notification.enabled and event_sink is not None:

        def _make_notification_tools(session_id: str) -> list[Any]:
            return build_notification_tools(
                settings.notification,
                event_sink=event_sink,
                session_id=session_id,
            )

        req_factories.append(_make_notification_tools)

    if not req_factories:
        return None

    def _compose(session_id: str) -> list[Any]:
        result: list[Any] = []
        for f in req_factories:
            result.extend(f(session_id))
        return result

    return _compose


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

    *bare* (default ``False``) skips skill injection, feature tools,
    subsession wiring, and memory — the agent gets a ``NullMemory`` and no
    tools.  Use it for bounded text-transformation calls (e.g. the
    ``POST /summary`` agent).

    Subsession wiring (*subsession_env*):

    * **Main chat agent** — pass *subsession_env* with ``subsession_ctx=None``.
      Per-request tools are built via ``request_tools_factory`` so each tool
      closure captures the owning ``session_id`` lexically.
    * **Subsession agent** — pass *subsession_env*, the worker's
      *subsession_ctx*, and its *subsession_close_state*.  The depth-aware
      tools (including ``complete_subsession``) are baked in statically;
      identity is fixed at construction.
    * ``None`` (default) — no subsession tools.

    *event_sink* is forwarded to :class:`~robotsix_chat.llm.LlmioChatAgent`
    for live tool/thinking activity frames on the ``GET /events`` channel.
    Pass the same :class:`~robotsix_chat.chat.events.EventBus` given to
    ``create_app`` — typically only for the main chat agent.
    """
    if settings is None:
        settings = Settings.load()
    if instruction is None:
        instruction = settings.agent_instruction

    instruction = _inject_skills(settings, instruction, bare=bare)

    effective_level = (
        model_level if model_level is not None else settings.llmio_model_level
    )
    api_key = (
        settings.llmio_api_key.get_secret_value()
        if level_needs_api_key(effective_level)
        else ""
    )

    tools = _build_static_tools(
        settings, bare=bare, conversation_store=conversation_store
    )
    if tool_wrapper is not None:
        tools = tool_wrapper(tools)

    request_tools_factory: Callable[[str], list[Any]] | None = None
    if not bare:
        if subsession_env is not None and subsession_ctx is not None:
            # Subsession agent: identity fixed at construction.
            from robotsix_chat.subsessions import build_subsession_tools

            tools.extend(
                build_subsession_tools(
                    subsession_env,
                    ctx=subsession_ctx,
                    close_state=subsession_close_state,
                )
            )
        # Build per-request tools factory — subsession tools for the main
        # agent, notification tools for both main and subsession agents.
        request_tools_factory = _build_request_tools_factory(
            settings,
            subsession_env if subsession_ctx is None else None,
            event_sink,
        )

    return LlmioChatAgent(
        model_level=effective_level,
        instruction=instruction,
        api_key=api_key,
        memory=NullMemory() if bare else build_memory(settings.memory),
        tools=tools,
        request_tools_factory=request_tools_factory,
        event_sink=event_sink,
    )
