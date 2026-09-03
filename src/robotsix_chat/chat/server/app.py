"""Chat SSE server — application factory and agent construction.

``create_app`` wires middlewares, routes, lifespan, and all shared state
into a Starlette ASGI application.  ``create_agent_from_settings`` builds
the LLM agent with its full tool suite from configuration.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
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
from robotsix_chat.config import Settings
from robotsix_chat.config.models import (
    EvergoingSettings,
    HealthSettings,
    MemoryComponentSettings,
)
from robotsix_chat.continuation import (
    build_continuation_tools,
    load_continuation_skill,
)
from robotsix_chat.diagnostics import build_diagnostics_tools
from robotsix_chat.docker_digest import (
    build_docker_digest_tools,
    load_docker_digest_skill,
)
from robotsix_chat.epic import build_decompose_epic_tool, load_epic_skill
from robotsix_chat.file_hub_tools import build_file_hub_tools, load_file_hub_skill
from robotsix_chat.gateway_route import (
    build_gateway_route_tools,
    load_gateway_route_skill,
)
from robotsix_chat.http_probe import build_http_probe_tools, load_http_probe_skill
from robotsix_chat.knowledge import build_knowledge_tools
from robotsix_chat.langfuse import (
    build_langfuse_inspect_tools,
    load_langfuse_inspect_skill,
)
from robotsix_chat.lifecycle import build_lifecycle_tools, load_lifecycle_skill
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.memory import ChatMemory, NullMemory, ReadOnlyMemory, build_memory
from robotsix_chat.notification import build_notification_tools, load_notification_skill
from robotsix_chat.public_fetch import build_public_fetch_tools, load_public_fetch_skill
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.render_url import build_render_url_tools, load_render_url_skill
from robotsix_chat.repo.actions import (
    build_github_actions_tools,
    load_github_actions_skill,
)
from robotsix_chat.repo.direct import (
    build_direct_repo_tools,
    load_direct_repo_skill,
)
from robotsix_chat.repo.security import build_github_security_tools, load_github_skill
from robotsix_chat.repo.study import build_repo_study_tools
from robotsix_chat.selfreview import build_recent_activity_tools
from robotsix_chat.sftp import build_sftp_tools
from robotsix_chat.skill_index import build_skill_index
from robotsix_chat.ticket_poll import (
    build_file_ticket_tool,
    build_find_ticket_by_pr_tool,
    build_list_stale_ready_tickets_tool,
    build_mark_ticket_done_tool,
    build_mark_ticket_ready_tool,
    build_merge_pull_request_tool,
    build_prioritize_all_open_tickets_tool,
    build_resolve_repo_tool,
    build_ticket_poll_tools,
    build_transition_ticket_tool,
    load_ticket_poll_skill,
)
from robotsix_chat.version_check import build_version_check_tools
from robotsix_chat.volume_tools import build_volume_tools, load_volume_tools_skill

from .idempotency import MessageIdempotencyStore
from .routes import (
    ChatAgent,
    MessageCoalescer,
    RunSerializer,
    auth_callback_endpoint,
    auth_login_endpoint,
    cancel_queued_endpoint,
    chat_endpoint,
    chat_skill_endpoint,
    config_deploy_get_endpoint,
    config_get_endpoint,
    config_rollback_endpoint,
    config_save_endpoint,
    config_version_diff_endpoint,
    config_version_get_endpoint,
    config_versions_endpoint,
    diagnostics_create_endpoint,
    diagnostics_list_endpoint,
    disk_usage_endpoint,
    draft_get_endpoint,
    draft_save_endpoint,
    events_endpoint,
    github_actions_secret_endpoint,
    github_actions_workflow_endpoint,
    github_job_log_endpoint,
    github_repo_create_endpoint,
    github_settings_endpoint,
    health_endpoint,
    history_endpoint,
    http_exception_handler,
    memory_ingestion_structure_endpoint,
    metrics_endpoint,
    mill_events_endpoint,
    mobile_token_endpoint,
    models_list_endpoint,
    not_found_handler,
    notifications_read_endpoint,
    notifications_unread_endpoint,
    periodic_definitions_list_endpoint,
    periodic_definitions_run_endpoint,
    prune_endpoint,
    server_error_handler,
    session_model_set_endpoint,
    sessions_close_endpoint,
    sessions_create_endpoint,
    sessions_delete_endpoint,
    sessions_list_endpoint,
    subsessions_close_endpoint,
    subsessions_get_endpoint,
    subsessions_list_endpoint,
    subsessions_message_endpoint,
    subsessions_transcript_endpoint,
    ui_endpoint,
    unhandled_exception_handler,
)

if TYPE_CHECKING:
    from robotsix_chat.config.models import (
        CentralDeploySettings,
        DirectRepoSettings,
        GitHubActionsSettings,
        GitHubSecuritySettings,
        MobileAuthSettings,
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
    app: Starlette | None = None,
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
    # Start the health-check scheduler if attached (created in create_app).
    if app is not None:
        scheduler = getattr(app.state, "health_scheduler", None)
        if scheduler is not None:
            try:
                scheduler.start()
            except Exception:
                logger.exception("Health scheduler start failed — continuing")
        evergoing_scheduler = getattr(app.state, "evergoing_scheduler", None)
        if evergoing_scheduler is not None:
            try:
                evergoing_scheduler.start()
            except Exception:
                logger.exception("Evergoing scheduler start failed — continuing")
        periodic_scheduler = getattr(app.state, "periodic_scheduler", None)
        if periodic_scheduler is not None:
            try:
                periodic_scheduler.start()
            except Exception:
                logger.exception("Periodic scheduler start failed — continuing")
    try:
        yield
    finally:
        if app is not None:
            scheduler = getattr(app.state, "health_scheduler", None)
            if scheduler is not None:
                try:
                    await scheduler.stop()
                except Exception:
                    logger.exception("Health scheduler stop failed")
            evergoing_scheduler = getattr(app.state, "evergoing_scheduler", None)
            if evergoing_scheduler is not None:
                try:
                    await evergoing_scheduler.stop()
                except Exception:
                    logger.exception("Evergoing scheduler stop failed")
            periodic_scheduler = getattr(app.state, "periodic_scheduler", None)
            if periodic_scheduler is not None:
                try:
                    await periodic_scheduler.close()
                except Exception:
                    logger.exception("Periodic scheduler stop failed")
            coalescer = getattr(app.state, "message_coalescer", None)
            if coalescer is not None:
                try:
                    await coalescer.close()
                except Exception:
                    logger.exception("MessageCoalescer drain failed")
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
        "compaction_keep_recent_turns",
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
        "periodic_definitions",
        "periodic_agent_factory",
        "periodic_state_path",
        "on_startup",
        "on_startup_async",
        "on_shutdown",
        "direct_repo_settings",
        "central_deploy_settings",
        "github_security_settings",
        "github_actions_settings",
        "config_path",
        "diagnostic_store",
        "knowledge_store",
        "health_settings",
        "evergoing_settings",
        "continuation_store",
        "notification_store",
    }
)


def create_app(
    agent: ChatAgent,
    *,
    summary_agent: ChatAgent | None = None,
    serve_ui: bool = True,
    idle_timeout_minutes: int = 30,
    compaction_min_turns: int = 3,
    compaction_keep_recent_turns: int = 2,
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
    periodic_definitions: list[Any] | None = None,
    periodic_agent_factory: Callable[[int | None], ChatAgent] | None = None,
    periodic_state_path: str | None = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
    direct_repo_settings: DirectRepoSettings | None = None,
    central_deploy_settings: CentralDeploySettings | None = None,
    github_security_settings: GitHubSecuritySettings | None = None,
    github_actions_settings: GitHubActionsSettings | None = None,
    mobile_auth: MobileAuthSettings | None = None,
    config_path: str | None = None,
    draft_store_dir: str | None = None,
    diagnostic_store: Any = None,
    knowledge_store: Any = None,
    health_settings: HealthSettings | None = None,
    evergoing_settings: EvergoingSettings | None = None,
    memory_component_settings: MemoryComponentSettings | None = None,
    continuation_store: Any = None,
    notification_store: Any = None,
    notification_store_and_forward: bool = True,
) -> Starlette:
    """Return a Starlette ASGI app wired to ``agent``.

    The returned app is a fully-initialised ASGI application that can be
    mounted directly in tests via ``httpx.ASGITransport`` or passed to
    ``uvicorn.run()``.

    Args:
        agent: Object whose ``stream(message)`` yields response tokens.
        summary_agent: Dedicated summariser agent used for the idle-timeout
            compaction summary, the carryover summary and conversation
            titles. ``None`` (default) reuses *agent* — pass a separate
            agent built on ``settings.summary_model_level`` with the
            summariser system prompt (see ``cli.py``) so those bounded
            text-transformation calls neither pay for the main agent's tier
            nor inherit its tool-oriented instructions.
        serve_ui: When ``True`` (default), serve the bundled browser chat
            UI at ``GET /`` so the UI and ``/chat`` share one origin.
        idle_timeout_minutes: Minutes of no user activity before the UI
            auto-restarts the conversation; ``0`` disables.
        compaction_min_turns: Minimum fresh (not yet summarized) turns a
            conversation needs before an idle timeout triggers in-place
            compaction; below this the summary agent is not invoked.
        compaction_keep_recent_turns: Number of the most recent turns left
            verbatim in the agent-facing replay after compaction.  This is
            what keeps a pending proposal and its exact identifiers intact;
            the summary only covers turns older than this window.
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
        periodic_definitions: Enabled
            :class:`~robotsix_chat.config.periodic_models.PeriodicSessionDefinition`
            presets. When ``None``/empty, no periodic scheduler is created.
        periodic_agent_factory: Callable mapping a preset's ``model_level``
            (``None`` for the global default) to the agent that runs its
            turns. When ``None``, the main *agent* runs every periodic turn.
        periodic_state_path: Override for the scheduler's firing-state file
            (tests); defaults to the standard /data location.
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
        central_deploy_settings: Canonical deploy-plane settings (URL and
            API key) used by the GitHub security/actions endpoints for
            inbound ``X-API-Key`` matching.  When ``None``, those
            endpoints return 503.
        github_security_settings: GitHub security-feature toggle config
            (org, deploy API key) used by the
            ``PATCH /chat/github/repos/{owner}/{repo}/settings`` endpoint.
            When ``None``, the endpoint returns 503.
        github_actions_settings: GitHub Actions config (org, deploy API key)
            used by the Actions secrets and workflow dispatch endpoints.
            When ``None``, the endpoints return 503.
        mobile_auth: Mobile SSO authentication settings.  When ``None``
            or disabled, the auth endpoints return 404.
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
        diagnostic_store: Shared :class:`~robotsix_chat.diagnostics.DiagnosticStore`
            instance, used by the ``POST /diagnostics/events`` and
            ``GET /diagnostics/events`` endpoints.  When ``None`` (default),
            the diagnostic endpoints return 503.
        knowledge_store: Shared :class:`~robotsix_chat.knowledge.KnowledgeStore`
            instance used for session carryover persistence.  When ``None``
            (default), carryover is disabled.
        health_settings: Optional
            :class:`~robotsix_chat.config.models.HealthSettings` controlling
            the periodic health-check scheduler.  When ``None`` (default),
            the default settings are used (enabled, 300 s interval).
        evergoing_settings: Optional
            :class:`~robotsix_chat.config.models.EvergoingSettings` controlling
            the single never-ending session and its periodic summarising
            compaction scheduler.  When ``None`` (default), the default
            settings are used (disabled), so no evergoing session is created
            on boot.
        memory_component_settings: Optional
            :class:`~robotsix_chat.config.models.MemoryComponentSettings` —
            summary pushes to the robotsix-memory component.
        continuation_store: Shared
            :class:`~robotsix_chat.continuation.store.ContinuationStore`
            instance for pending post-restart continuations.  When
            ``None`` (default), the chat endpoint does not reset the
            consecutive-continuation guardrail counter on operator messages.
        notification_store: Shared
            :class:`~robotsix_chat.notification.store.NotificationStore`
            persisting ``notify_user`` notifications to chat-data so they
            survive a disconnected browser.  When ``None`` (default),
            notifications are published live but not persisted.
        notification_store_and_forward: Feature flag mirroring
            ``notification.store_and_forward``.  When ``False``, the
            ``/notifications`` API endpoints return empty responses (the
            emergency kill-switch); ``True`` (default) serves persisted
            records.

    """
    routes: list[Route | Mount] = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
        Route("/auth/login", auth_login_endpoint, methods=["GET"]),
        Route("/auth/callback", auth_callback_endpoint, methods=["GET"]),
        Route(
            "/chat/auth/mobile-token",
            mobile_token_endpoint,
            methods=["POST"],
        ),
        Route("/admin/disk", disk_usage_endpoint, methods=["GET"]),
        Route("/admin/prune", prune_endpoint, methods=["POST"]),
        Route(
            "/admin/memory/ingestion-structure",
            memory_ingestion_structure_endpoint,
            methods=["POST"],
        ),
        Route("/mill-events", mill_events_endpoint, methods=["POST"]),
        Route("/chat", chat_endpoint, methods=["POST"]),
        Route("/chat/queue/cancel", cancel_queued_endpoint, methods=["POST"]),
        Route("/events", events_endpoint, methods=["GET"]),
        Route("/history", history_endpoint, methods=["GET"]),
        Route("/models", models_list_endpoint, methods=["GET"]),
        Route(
            "/notifications/unread",
            notifications_unread_endpoint,
            methods=["GET"],
        ),
        Route(
            "/notifications/read",
            notifications_read_endpoint,
            methods=["POST"],
        ),
        Route("/sessions", sessions_list_endpoint, methods=["GET"]),
        Route("/sessions", sessions_create_endpoint, methods=["POST"]),
        Route(
            "/sessions/{session_id}/model",
            session_model_set_endpoint,
            methods=["POST"],
        ),
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
            "/periodic/definitions",
            periodic_definitions_list_endpoint,
            methods=["GET"],
        ),
        Route(
            "/periodic/definitions/{name}/run",
            periodic_definitions_run_endpoint,
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
            "/chat/github/repos",
            github_repo_create_endpoint,
            methods=["POST"],
        ),
        Route(
            "/chat/github/repos/{owner}/{repo}/settings",
            github_settings_endpoint,
            methods=["PATCH"],
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
        Route(
            "/chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            github_job_log_endpoint,
            methods=["GET"],
        ),
        Route("/chat-skill", chat_skill_endpoint, methods=["GET"]),
        Route("/config", config_get_endpoint, methods=["GET"]),
        Route("/config/deploy", config_deploy_get_endpoint, methods=["GET"]),
        Route("/config", config_save_endpoint, methods=["PUT"]),
        Route("/config/versions", config_versions_endpoint, methods=["GET"]),
        Route(
            "/config/versions/{version:int}",
            config_version_get_endpoint,
            methods=["GET"],
        ),
        Route(
            "/config/versions/{version:int}/diff",
            config_version_diff_endpoint,
            methods=["GET"],
        ),
        Route("/config/rollback", config_rollback_endpoint, methods=["POST"]),
        Route(
            "/diagnostics/events",
            diagnostics_create_endpoint,
            methods=["POST"],
        ),
        Route(
            "/diagnostics/events",
            diagnostics_list_endpoint,
            methods=["GET"],
        ),
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
            app=_app,
        ),
    )
    app.state.agent = agent
    # The main agent's memory backend, surfaced for GET /health so a frozen
    # store is externally visible (``None`` for agents with no memory).
    app.state.memory = getattr(agent, "memory", None)
    app.state.summary_agent = summary_agent if summary_agent is not None else agent
    app.state.conversation_store = conversation_store or ConversationStore()
    # Configured chat level — the baseline a session runs at until the agent
    # escalates it. Read from the agent so create_app needs no new parameter.
    app.state.chat_model_level = getattr(agent, "model_level", None)
    app.state.llmio_tier_overrides = getattr(agent, "tier_overrides", None)
    # Whether keyed (OpenRouter) model levels are usable — surfaced to the UI
    # model selector so it can mark levels that need an absent API key as
    # unavailable. Read from the agent so create_app needs no new parameter.
    app.state.chat_api_key_available = bool(getattr(agent, "has_api_key", False))
    app.state.idle_timeout_minutes = idle_timeout_minutes
    app.state.compaction_min_turns = compaction_min_turns
    app.state.compaction_keep_recent_turns = compaction_keep_recent_turns
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
    app.state.central_deploy_settings = central_deploy_settings
    app.state.github_security_settings = github_security_settings
    app.state.github_actions_settings = github_actions_settings
    app.state.mobile_auth = mobile_auth
    app.state.feedback_runner = feedback_runner  # may be None
    if periodic_definitions:
        from robotsix_chat.periodic import (
            PERIODIC_OWNER,
            PERIODIC_SCHEDULER_PERSIST_PATH,
            PeriodicScheduler,
        )

        _p_coalescer = app.state.message_coalescer
        _p_store = app.state.conversation_store

        async def _periodic_submit_turn(
            session_id: str, message: str, model_level: int | None
        ) -> None:
            """Run one periodic turn through the normal chat submit path."""
            turn_agent = (
                periodic_agent_factory(model_level)
                if periodic_agent_factory is not None
                else app.state.agent
            )
            queue = await _p_coalescer.submit(
                session_id,
                message,
                None,
                None,
                agent=turn_agent,
                store=_p_store,
                run_serializer=app.state.run_serializer,
                msg_id_store=app.state.msg_id_store,
                lock_key=session_id,
                owner_id=PERIODIC_OWNER,
                had_session=True,
                summary_agent=app.state.summary_agent,
                event_bus=app.state.event_bus,
            )
            # Drain to completion so the scheduler's task tracks the whole
            # turn (there is no SSE client on a scheduled firing).
            from .routes.constants import SSE_DONE_TYPE, SSE_ERROR_TYPE

            while True:
                frame_type, _payload = await queue.get()
                if frame_type in (SSE_DONE_TYPE, SSE_ERROR_TYPE):
                    break

        app.state.periodic_scheduler = PeriodicScheduler(
            definitions=periodic_definitions,
            conversation_store=_p_store,
            submit_turn=_periodic_submit_turn,
            is_busy=_p_coalescer.is_busy,
            persist_path=periodic_state_path or PERIODIC_SCHEDULER_PERSIST_PATH,
        )
    else:
        app.state.periodic_scheduler = None
    app.state.diagnostic_store = diagnostic_store  # may be None
    app.state.knowledge_store = knowledge_store  # may be None
    # Health-check scheduler — created here so it has access to the fully
    # populated app.state, then started during the lifespan async phase.
    _hs = health_settings or HealthSettings()
    app.state.health_settings = _hs
    if _hs.enabled:
        from robotsix_chat.health import HealthScheduler

        app.state.health_scheduler = HealthScheduler(
            interval_seconds=_hs.check_interval_seconds,
            state=app.state,
        )
    else:
        app.state.health_scheduler = None
    app.state.health_status = None  # populated after first check cycle
    # Evergoing session — the single never-ending session.  When enabled, it
    # is created on boot (idempotent) and a periodic subject-aware trim
    # scheduler runs against it; started during the lifespan async phase.
    _eg = evergoing_settings or EvergoingSettings()
    if _eg.enabled:
        from robotsix_chat.chat.conversation import OPERATOR_OWNER

        app.state.conversation_store.ensure_evergoing_session(OPERATOR_OWNER)
    # The periodic summary scheduler is the single context-reduction
    # mechanism for ALL sessions (idle compaction removed), so it runs
    # regardless of whether the evergoing session itself is enabled.
    if app.state.summary_agent is not None:
        from robotsix_chat.evergoing import EvergoingSummaryScheduler
        from robotsix_chat.memory_push import MemoryPush

        _mc = memory_component_settings or MemoryComponentSettings()
        memory_push = (
            MemoryPush(_mc.url, timeout_seconds=_mc.timeout_seconds)
            if _mc.enabled
            else None
        )
        app.state.evergoing_scheduler = EvergoingSummaryScheduler(
            interval_seconds=_eg.trim_interval_seconds,
            store=app.state.conversation_store,
            agent=app.state.summary_agent,
            keep_recent_runs=_eg.keep_recent_runs,
            memory_push=memory_push,
        )
    else:
        app.state.evergoing_scheduler = None
    app.state.continuation_store = continuation_store  # may be None
    app.state.notification_store = notification_store  # may be None
    app.state.notification_store_and_forward = notification_store_and_forward
    if config_path is not None:
        app.state.config_path = config_path
    if draft_store_dir is not None:
        app.state.draft_store_dir = draft_store_dir
    return app


# Canonical path to the reply-style directive — the single source of truth
# for how the agent formats replies.  The file is read at agent construction
# time and appended to the system prompt on every build.  It lives INSIDE
# the package (like the per-component skill.md files) so the deployed image
# actually contains it: the old CWD-relative ``docs/prompt-style.md`` did
# not ship, and production silently ran without a style directive from the
# feature's introduction until 2026-09-01.
_PROMPT_STYLE_PATH = Path(__file__).parent / "prompt-style.md"

# Delimiter line that separates the header/description from the actual
# directive in the style file.  Everything after this line (exclusive)
# is injected into the system prompt.
_STYLE_DIRECTIVE_HEADER = "## Style directive"


def _load_prompt_style() -> str:
    """Read the canonical reply-style directive from disk.

    Returns the directive text (the content following the
    ``## Style directive`` header in the packaged ``prompt-style.md``), or
    an empty string if the file is missing — a missing file logs
    a warning but is not fatal (the agent runs without a style
    directive).
    """
    try:
        raw = _PROMPT_STYLE_PATH.read_text()
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            "Prompt style file not found at %s — agent will run "
            "without a reply-style directive.",
            _PROMPT_STYLE_PATH,
        )
        return ""
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Could not read prompt style file at %s: %s — agent will "
            "run without a reply-style directive.",
            _PROMPT_STYLE_PATH,
            exc,
        )
        return ""

    # Extract the directive section: everything after the
    # "## Style directive" header line.
    header_idx = raw.find(_STYLE_DIRECTIVE_HEADER)
    if header_idx == -1:
        logging.getLogger(__name__).warning(
            "Prompt style file at %s is missing the %r header — "
            "agent will run without a reply-style directive.",
            _PROMPT_STYLE_PATH,
            _STYLE_DIRECTIVE_HEADER,
        )
        return ""

    # Drop the header line itself and any blank lines immediately after it.
    body = raw[header_idx + len(_STYLE_DIRECTIVE_HEADER) :]
    body = body.lstrip("\n").strip()
    if not body:
        logging.getLogger(__name__).warning(
            "Prompt style file at %s has an empty directive section — "
            "agent will run without a reply-style directive.",
            _PROMPT_STYLE_PATH,
        )
        return ""

    return body


def _skill_registry(
    settings: Settings,
) -> list[tuple[bool, str, Callable[[], str]]]:
    """Return ``(enabled, name, loader)`` for every bundled skill.

    Single source of truth for both halves of progressive disclosure: the
    index built into the system prompt and the ``read_skill`` tool that
    serves bodies on demand. Keeping them on one list is what stops the
    index from advertising a skill the tool cannot fetch.
    """
    from robotsix_chat.evergoing import load_cross_session_skill
    from robotsix_chat.subsessions import load_subsessions_skill

    return [
        (True, "subsessions", load_subsessions_skill),
        (
            settings.evergoing.enabled,
            "evergoing_cross_session",
            load_cross_session_skill,
        ),
        (settings.lifecycle.enabled, "lifecycle", load_lifecycle_skill),
        (settings.notification.enabled, "notification", load_notification_skill),
        (settings.http_probe.enabled, "http_probe", load_http_probe_skill),
        (settings.docker_digest.enabled, "docker_digest", load_docker_digest_skill),
        (settings.gateway_route.enabled, "gateway_route", load_gateway_route_skill),
        (
            settings.continuation.enabled,
            "continuation",
            load_continuation_skill,
        ),
        (
            settings.langfuse_inspect.enabled,
            "langfuse_inspect",
            load_langfuse_inspect_skill,
        ),
        (settings.public_fetch.enabled, "public_fetch", load_public_fetch_skill),
        (settings.render_url.enabled, "render_url", load_render_url_skill),
        (settings.github_security.enabled, "github_security", load_github_skill),
        (settings.github_actions.enabled, "github_actions", load_github_actions_skill),
        (settings.direct_repo.enabled, "direct_repo", load_direct_repo_skill),
        (settings.volume_tools.enabled, "volume_tools", load_volume_tools_skill),
        (settings.file_hub_tools.enabled, "file_hub_tools", load_file_hub_skill),
        (
            bool(settings.direct_repo.board_api_base_url.strip())
            or bool(settings.central_deploy.url.strip()),
            "ticket_poll",
            load_ticket_poll_skill,
        ),
        (
            bool(settings.direct_repo.board_api_base_url.strip())
            or bool(settings.central_deploy.url.strip()),
            "epic",
            load_epic_skill,
        ),
    ]


def _current_datetime_directive(now: datetime | None = None) -> str:
    """Return the authoritative current-date/time directive.

    The agent has no reliable internal clock: date-relative reasoning
    ("no meeting in the last 7 days", "an upcoming earnings date", "the
    scheduler missed a trigger") is unreliable unless an authoritative
    timestamp is injected into its context.  This directive stamps the
    build-time UTC clock into the system prompt and tells the agent to treat
    it — not its own assumptions — as the source of truth for any
    date-relative conclusion, so it never raises a false "missed event"
    alarm when the scheduled time simply has not arrived yet.

    *now* defaults to :func:`datetime.now` in UTC; it is injectable so tests
    can pin a deterministic value.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "## Current date and time (authoritative)\n"
        f"The current date and time is {stamp} (UTC). This clock signal was "
        "injected into your context when this session started; treat it — not "
        "your own assumptions or training-cutoff intuition — as the source of "
        "truth for every date-relative conclusion (e.g. days since an event, "
        "whether a deadline has passed, whether a scheduled meeting or "
        "earnings date is upcoming or overdue, whether a scheduler missed a "
        "trigger). Never conclude that a scheduled event was missed, overdue, "
        "or skipped unless this current date/time is strictly after the "
        "event's scheduled time."
    )


def _inject_skills(
    settings: Settings,
    instruction: str,
    *,
    bare: bool = False,
    now: datetime | None = None,
) -> str:
    """Augment *instruction* with component-access instructions and skill prompts.

    Skill injection is disabled when *bare* is ``True``, but the canonical
    reply-style directive and the authoritative current-date/time directive
    are always appended — they apply to every agent build, not just
    tool-enabled ones.  Each skill gate is independently gated by its own
    settings key (``central_deploy.url``, ``lifecycle.enabled``,
    ``notification.enabled``, ``github_security.enabled``).

    *now* is forwarded to :func:`_current_datetime_directive` so tests can pin
    the injected timestamp.
    """
    # Always append the authoritative current-date/time directive so the
    # agent has a reliable clock signal for date-relative reasoning on every
    # build (interactive, bare summariser, and unattended periodic agents).
    instruction = f"{instruction}\n\n{_current_datetime_directive(now)}"

    # Always append the canonical reply-style directive — this is a
    # formatting directive, not a skill, and applies to every agent build.
    style = _load_prompt_style()
    if style:
        instruction = f"{instruction}\n\n{style}"

    if bare:
        return instruction

    # Central-deploy roster — component-access instruction + skill prompts.
    if settings.central_deploy.url:
        instruction = (
            f"{instruction}\n\n"
            "Component access:\n"
            "– You have one generic tool for calling external components: "
            "component_request(component_id, method, path, json_body=None, "
            "params=None). "
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

    _skill_entries = _skill_registry(settings)

    index = build_skill_index(
        [(name, loader) for enabled, name, loader in _skill_entries if enabled]
    )
    if index:
        instruction = f"{instruction}\n\n{index}"

    return instruction


def build_skill_tools(settings: Settings) -> list[Callable[..., Any]]:
    """Return the ``read_skill`` tool — the read half of progressive disclosure.

    The system prompt carries only a summary per skill (see
    :mod:`robotsix_chat.skill_index`); this serves the full body when the agent
    decides it needs one. Both halves read the same registry, so the tool can
    always fetch anything the index advertises.
    """
    registry = {
        name: loader for enabled, name, loader in _skill_registry(settings) if enabled
    }
    # Cache successfully loaded skill bodies so we can serve a stale copy if a
    # later load fails (transient I/O error, file locked, etc.).
    _skill_body_cache: dict[str, str] = {}

    def read_skill(name: str) -> str:
        """Read the full instructions for one of your available skills.

        Call this before acting on a capability listed in "Available skills" —
        the prompt carries only a one-line summary of each, and the body holds
        the endpoints, arguments, safety rules and examples. The body stays in
        context afterwards, so read each skill at most once per session.

        Args:
            name: Skill name exactly as listed in "Available skills".

        Returns:
            The skill's full Markdown body, or an error naming the valid
            skills when *name* is not one of them.

        """
        loader = registry.get(name)
        if loader is None:
            available = ", ".join(sorted(registry)) or "(none)"
            return (
                f"No skill named {name!r}. Available skills: {available}. "
                "Use the name exactly as listed in 'Available skills'."
            )

        # Retry up to 2 times with exponential backoff for transient failures.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                body = loader()
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    logger.warning(
                        "read_skill(%s) attempt %d failed, retrying",
                        name,
                        attempt + 1,
                        exc_info=True,
                    )
                    time.sleep(0.1 * (2**attempt))
                continue
            else:
                # Success — cache and return.
                if body:
                    _skill_body_cache[name] = body
                return body or f"Skill {name!r} is registered but its body is empty."

        # All retries exhausted — try the cache.
        logger.warning("read_skill(%s) failed after 3 attempts", name, exc_info=True)
        cached = _skill_body_cache.get(name)
        if cached:
            logger.info("read_skill(%s) serving stale cached body", name)
            return cached

        return (
            f"Skill {name!r} could not be loaded after multiple attempts: "
            f"{last_exc}. "
            "Inform the user that this skill's instructions are temporarily "
            "unavailable. Offer to proceed based on general knowledge or ask "
            "the user to retry later."
        )

    return [read_skill]


def _build_static_tools(
    settings: Settings,
    *,
    bare: bool = False,
    conversation_store: ConversationStore | None = None,
    diagnostic_store: Any = None,
    knowledge_store: Any = None,
    continuation_store: Any = None,
) -> list[Any]:
    """Return the static (non-per-request) tool suite gated by *settings*.

    When *bare* is ``True`` returns an empty list — the agent gets no tools.
    """
    if bare:
        return []

    component_access_tools = build_component_access_tools(settings.central_deploy)
    component_request = component_access_tools[0] if component_access_tools else None

    from robotsix_chat.llm.tool_utils import require_args

    raw_tools = [
        *build_skill_tools(settings),
        *component_access_tools,
        *build_component_tools(settings.component_client),
        *build_refdocs_tools(settings.refdocs, settings.direct_repo),
        *build_repo_study_tools(
            settings.repo_study,
            settings.direct_repo,
            diagnostic_store=diagnostic_store,
        ),
        *build_direct_repo_tools(
            settings.direct_repo,
            component_request=component_request,
            file_hub_work_dir=settings.file_hub_tools.working_dir,
        ),
        *build_github_security_tools(settings.github_security, settings.direct_repo),
        *build_github_actions_tools(settings.github_actions, settings.direct_repo),
        *build_knowledge_tools(settings.knowledge, store=knowledge_store),
        *build_continuation_tools(
            settings.continuation, continuation_store=continuation_store
        ),
        *build_diagnostics_tools(settings.diagnostics, store=diagnostic_store),
        *build_recent_activity_tools(settings.self_review, conversation_store),
        *build_version_check_tools(settings.version_check, settings.direct_repo),
        *build_lifecycle_tools(settings.lifecycle, settings.central_deploy.url),
        *build_render_url_tools(settings.render_url),
        *build_http_probe_tools(settings.http_probe, settings.central_deploy),
        *build_docker_digest_tools(settings.docker_digest),
        *build_gateway_route_tools(settings.gateway_route, settings.central_deploy),
        *build_public_fetch_tools(settings.public_fetch, settings.central_deploy),
        *build_langfuse_inspect_tools(settings.langfuse_inspect, settings.langfuse),
        *build_sftp_tools(settings.sftp),
        *build_file_hub_tools(settings.file_hub_tools),
        *build_volume_tools(settings.volume_tools),
        *build_ticket_poll_tools(settings, component_request=component_request),
        *build_merge_pull_request_tool(settings, component_request=component_request),
        *build_file_ticket_tool(settings, component_request=component_request),
        *build_mark_ticket_ready_tool(settings, component_request=component_request),
        *build_transition_ticket_tool(settings, component_request=component_request),
        *build_mark_ticket_done_tool(settings, component_request=component_request),
        *build_find_ticket_by_pr_tool(settings, component_request=component_request),
        *build_resolve_repo_tool(settings, component_request=component_request),
        *build_prioritize_all_open_tickets_tool(
            settings, component_request=component_request
        ),
        *build_list_stale_ready_tickets_tool(
            settings, component_request=component_request
        ),
        *build_decompose_epic_tool(settings, component_request=component_request),
    ]

    # Capture tool names for the readiness-check tool.  The snapshot is
    # taken before list_available_tools is appended so the count is of the
    # "real" tools; list_available_tools adds itself to the output.
    _tool_names = sorted({getattr(t, "__name__", type(t).__name__) for t in raw_tools})

    async def list_available_tools() -> str:
        """List all tools currently available to the agent.

        Returns a sorted, newline-separated list of tool names.  Use this
        after a restart to verify that expected tools (e.g. newly-added
        tools from an image update) are present before declaring them
        ready.  If an expected tool is missing, the image may not have
        been updated — redeploy and verify via the lifecycle tools.

        Returns:
            A formatted list of available tool names.

        """
        return (
            f"Available tools ({len(_tool_names) + 1}):\n"
            + "\n".join(f"  - {name}" for name in _tool_names)
            + "\n  - list_available_tools"
        )

    raw_tools.append(list_available_tools)

    return [require_args(t) for t in raw_tools]


def _build_request_tools_factory(
    settings: Settings,
    subsession_env: SubsessionEnv | None,
    event_sink: EventSink | None,
    conversation_store: ConversationStore | None = None,
    configured_level: int | None = None,
    notification_store: Any = None,
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
                store=notification_store,
            )

        req_factories.append(_make_notification_tools)

    if settings.evergoing.enabled and conversation_store is not None:
        from robotsix_chat.evergoing import build_cross_session_tools

        cross_session_store = conversation_store

        def _make_cross_session_tools(session_id: str) -> list[Any]:
            return build_cross_session_tools(
                conversation_store=cross_session_store,
                session_id=session_id,
            )

        req_factories.append(_make_cross_session_tools)

    if conversation_store is not None and configured_level is not None:
        from robotsix_llmio.config import load_tier_config

        from robotsix_chat.llm.agent import _merge_tier_overrides
        from robotsix_chat.llm.escalation import build_escalation_tools

        store = conversation_store
        level = configured_level
        # Chat's own tier config (incl. llmio_tier_overrides) so the
        # escalation event names the model that actually serves the level.
        escalation_tier_config = load_tier_config(
            _merge_tier_overrides(settings.llmio_tier_overrides, None)
        )

        def _make_escalation_tools(session_id: str) -> list[Any]:
            return build_escalation_tools(
                conversation_store=store,
                session_id=session_id,
                configured_level=level,
                event_sink=event_sink,
                tier_config=escalation_tier_config,
            )

        req_factories.append(_make_escalation_tools)

    if not req_factories:
        return None

    def _compose(session_id: str) -> list[Any]:
        from robotsix_chat.llm.tool_utils import require_args

        result: list[Any] = []
        for f in req_factories:
            result.extend(f(session_id))
        return [require_args(t) for t in result]

    return _compose


# Standing, code-level instruction appended to the main chat agent's system
# prompt (never sourced from the operator-editable config document, so it
# holds for every session regardless of config edits).  It teaches the agent
# to emit the ``suggestions`` fenced block that the browser UI
# (``parseSuggestions`` / ``renderSuggestionChips`` in ``chat.js``) turns into
# clickable answer chips.
_SUGGESTIONS_INSTRUCTION = (
    "\n\n"
    "## Multiple-choice decisions — clickable answer chips\n"
    "When you present the operator with a discrete multiple-choice decision "
    "(approve/reject, Option A/B/C, yes/no, pick-one-of-N), END your message "
    "with a fenced block:\n"
    "```suggestions\n"
    "<one option per line>\n"
    "```\n"
    "Rules: use it ONLY for genuine discrete choices awaiting an operator "
    "reply — never for rhetorical lists or FYI enumerations; give 2-5 "
    "options; each line must be <= ~80 characters, self-contained and "
    'actionable as a verbatim reply (e.g. "Approve ticket 73f3 and merge", '
    'not "Option A"); and always phrase the surrounding prose so a typed '
    "free-text answer is equally valid."
)


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
    memory_enabled: bool = True,
    event_sink: EventSink | None = None,
    diagnostic_store: Any = None,
    knowledge_store: Any = None,
    continuation_store: Any = None,
    notification_store: Any = None,
) -> LlmioChatAgent:
    """Build an :class:`LlmioChatAgent` wired from *settings*.

    The backend is chosen by robotsix-llmio's capability level: *model_level*
    when given, else ``settings.chat_default_model_level``.  The level encodes the
    transport + model; ``settings.llmio_api_key`` is forwarded only when the
    effective level's transport needs a key (keyless claudeSDK levels 2, 4, 5
    never receive one).

    When *settings* is ``None``, ``Settings.load()`` resolves configuration
    from the YAML config file and environment. When *instruction* is ``None``,
    it is taken from ``settings.agent_instruction``.

    *bare* (default ``False``) skips skill injection, feature tools,
    subsession wiring, and memory — the agent gets a ``NullMemory`` and no
    tools.  Use it for bounded text-transformation calls (e.g. the
    compaction summary agent).

    *memory_enabled* (default ``True``) gates only long-term (cognee) memory
    while leaving tools and subsession wiring intact.  Set ``False`` for
    unattended background agents (subsession workers, periodic
    auto-continue) that would otherwise recall + cognify every turn around
    the clock; they get a ``NullMemory``.  The interactive main-chat agent
    keeps the default.  ``bare`` already implies no memory regardless.

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

    # The main chat agent (operator-attended, no fixed subsession identity)
    # gets the standing suggestion-chip contract so multiple-choice decisions
    # render as clickable answer buttons.  Bare text-transformation agents and
    # subsession children are excluded — subsessions get their own suggestion
    # directive via ``_USER_CHAT_FIRST_TURN_NOTE`` in the worker.
    if not bare and subsession_ctx is None:
        instruction = instruction + _SUGGESTIONS_INSTRUCTION

    effective_level = (
        model_level if model_level is not None else settings.chat_default_model_level
    )
    # Frontier subsessions (level 3) get an orchestration directive so they
    # delegate bulk reading/extraction to cheaper child subsessions by
    # default instead of burning frontier-model turns on it themselves.
    # Only subsession agents receive it — the main chat agent is
    # operator-attended and already interactive.
    if subsession_ctx is not None:
        from robotsix_chat.subsessions.models import (
            COSTLY_TIER_MIN_LEVEL,
            COSTLY_TIER_ORCHESTRATION_DIRECTIVE,
        )

        if effective_level >= COSTLY_TIER_MIN_LEVEL:
            instruction = instruction + COSTLY_TIER_ORCHESTRATION_DIRECTIVE
    # Always hand over the configured key. LlmioChatAgent forwards it only
    # to keyed (OpenRouter) slot attempts, and it needs to be holding it for
    # llmio's provider failover to reach the keyed fallback slot when the
    # shared Claude credential or quota is what failed — the mode that takes
    # the whole default slot down at once.
    api_key = settings.llmio_api_key.get_secret_value()

    tools = _build_static_tools(
        settings,
        bare=bare,
        conversation_store=conversation_store,
        diagnostic_store=diagnostic_store,
        knowledge_store=knowledge_store,
        continuation_store=continuation_store,
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
            # Subsession agents get notify_user pinned to the owner's session
            # so notifications reach the user's connected browser.  Built
            # statically here (not via the per-request factory) because the
            # per-request factory receives the subsession's own sub_id, not
            # the owner's session_id that the browser is subscribed to.
            if subsession_env.event_sink is not None:
                from robotsix_chat.notification import build_notification_tools

                tools.extend(
                    build_notification_tools(
                        settings.notification,
                        event_sink=subsession_env.event_sink,
                        session_id=subsession_ctx.owner_session_id,
                        store=notification_store,
                    )
                )
        # Build per-request tools factory — subsession tools for the main
        # agent, notification tools for the main agent.
        request_tools_factory = _build_request_tools_factory(
            settings,
            subsession_env if subsession_ctx is None else None,
            event_sink,
            # Escalation is a main-chat affordance only: subsessions already
            # choose their own level when spawned, and a periodic/bare
            # agent has no operator watching the badge change.
            conversation_store if subsession_ctx is None and not bare else None,
            (
                settings.chat_default_model_level
                if subsession_ctx is None and not bare
                else None
            ),
            notification_store=notification_store,
        )

    # Read/write split for background agents. Recall is a retrieval-only
    # lookup (~0.4 s warm, no LLM call); cognify is a multi-minute LLM
    # pipeline that also contends with every concurrent recall. So an agent
    # whose WRITE gate is off can still safely READ — denying it the context
    # the main conversation already learned buys nothing.
    if bare:
        memory: ChatMemory = NullMemory()
    elif memory_enabled:
        memory = build_memory(
            settings.memory,
            settings.langfuse,
            settings.openrouter,
            memory_component=settings.memory_component,
        )
    elif settings.memory_component.enabled or (
        settings.memory.enabled and settings.memory.background_recall_enabled
    ):
        memory = ReadOnlyMemory(
            build_memory(
                settings.memory,
                settings.langfuse,
                settings.openrouter,
                memory_component=settings.memory_component,
            )
        )
    else:
        memory = NullMemory()
    # Deep on-demand memory search: the automatic per-message recall is
    # retrieval-only and cheap; the expensive LLM-mediated graph search is a
    # tool the model invokes deliberately. Returns [] for NullMemory, so this
    # is safe unconditionally.
    from robotsix_chat.memory.tools import build_memory_tools

    tools.extend(build_memory_tools(memory))

    agent = LlmioChatAgent(
        model_level=effective_level,
        instruction=instruction,
        api_key=api_key,
        memory=memory,
        tools=tools,
        request_tools_factory=request_tools_factory,
        event_sink=event_sink,
        task_budget_tokens=settings.llmio_task_budget_tokens,
        failover_window_seconds=settings.llmio_failover_window_seconds,
        tier_overrides=settings.llmio_tier_overrides,
    )
    # Wire guarded auto-recovery (self-restart) for the top-level chat agent's
    # memory only — never for bare summary agents, subsession children, or any
    # agent whose memory was gated off (a NullMemory has nothing to recover).
    # It needs the lifecycle transport; without it a freeze is still surfaced
    # (via ERROR log + GET /health) but not auto-healed.
    if (
        not bare
        and memory_enabled
        and subsession_ctx is None
        and settings.lifecycle.enabled
    ):
        from robotsix_chat.lifecycle.client import LifecycleClient

        # The canonical deploy URL must be passed explicitly — constructing
        # the client without it left auto-recovery pointing at an empty base
        # URL, so every self-restart attempt failed with a URL protocol
        # error (and boot logged "central_deploy.url is empty" although the
        # config had it): a frozen memory backend was never auto-healed.
        agent.memory.set_recovery_callback(
            LifecycleClient(
                settings.lifecycle, settings.central_deploy.url
            ).self_restart
        )
    # Wire user-facing escalation (notify_user) for the top-level chat agent's
    # memory so a store fault auto-recovery cannot safely heal (e.g. a
    # graph-store open segfault whose on-disk copy will not open) surfaces to
    # the user rather than staying a silent log line.  Same gating as recovery.
    if (
        not bare
        and memory_enabled
        and subsession_ctx is None
        and settings.notification.enabled
        and event_sink is not None
    ):
        from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE

        _notify_sink = event_sink
        _notify_store = notification_store

        async def _escalate(title: str, body: str) -> None:
            """Broadcast a high-urgency notification to every connected browser."""
            frame: dict[str, object] = {
                "type": SSE_NOTIFICATION_TYPE,
                "title": title,
                "body": body,
                "urgency": "high",
                "link": "",
            }
            if _notify_store is not None:
                try:
                    _notify_store.append(title=title, body=body, source_session="")
                except Exception:
                    logger.exception(
                        "failed to persist memory-fault escalation notification"
                    )
            _notify_sink.publish_all(frame)

        agent.memory.set_notify_callback(_escalate)
    return agent
