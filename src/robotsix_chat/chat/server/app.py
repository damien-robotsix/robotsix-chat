"""Chat SSE server — application factory.

``create_app`` wires middlewares, routes, lifespan, and all shared state
into a Starlette ASGI application.  Agent construction lives in
:mod:`.agent_factory`, skill wiring in :mod:`.skill_wiring`, the OpenAPI
schema in :mod:`.openapi`, the UI loader in :mod:`.ui` and the structlog
middleware in :mod:`.middleware`.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from importlib import resources
from typing import TYPE_CHECKING, Any

from asgi_correlation_id import CorrelationIdMiddleware
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.config import DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES
from robotsix_chat.config.models import (
    EvergoingSettings,
    HealthSettings,
    MemoryComponentSettings,
)

from .idempotency import MessageIdempotencyStore
from .middleware import StructlogContextMiddleware
from .openapi import _API_ROUTE_SPECS, docs_endpoint, openapi_json_endpoint
from .routes import (
    ChatAgent,
    MessageCoalescer,
    RunSerializer,
    chat_skill_endpoint,
    http_exception_handler,
    not_found_handler,
    server_error_handler,
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
        ParentDelivery,
        SubsessionRegistry,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application factory & entry point
# ---------------------------------------------------------------------------


def _register_inflight_probes(coalescer: Any, subsession_registry: Any) -> None:
    """Wire the self-restart drain gate to this app's sources of agent turns.

    The main-chat coalescer carries operator and periodic turns; the
    subsession registry carries subsession workers mid-turn (``RUNNING`` —
    a ``user_chat`` waiting for the operator is idle and not counted).
    """
    from robotsix_chat.lifecycle.drain import register_inflight_probe
    from robotsix_chat.subsessions import SubsessionStatus

    def _chat_turns() -> int:
        if coalescer is None or not hasattr(coalescer, "inflight_count"):
            return 0
        return int(coalescer.inflight_count())

    def _running_subsessions() -> int:
        if subsession_registry is None:
            return 0
        return sum(
            1
            for info in subsession_registry.list_all()
            if info.status is SubsessionStatus.RUNNING
        )

    # Re-registering under the same names replaces a previous app's probes.
    register_inflight_probe("chat_turns", _chat_turns)
    register_inflight_probe("subsessions", _running_subsessions)


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
            the periodic summarising compaction scheduler (interval, runs
            kept verbatim).  When ``None`` (default), the default settings
            are used.
        memory_component_settings: Optional
            :class:`~robotsix_chat.config.models.MemoryComponentSettings` —
            summary pushes to the robotsix-memory component.
        continuation_store: Shared
            :class:`~robotsix_chat.continuation.store.ContinuationStore`
            instance for pending post-restart continuations.  When
            ``None`` (default), the chat endpoint does not reset the
            consecutive-continuation guardrail counter on operator messages.

    """
    routes: list[Route | Mount] = []
    for path, endpoint, methods, _summary, versioned in _API_ROUTE_SPECS:
        routes.append(Route(path, endpoint, methods=list(methods)))
        # Stable, versioned alias under /api/v1.  The historical root path
        # stays registered so existing un-versioned callers keep working
        # during the transition (only ops probes are root-only).
        if versioned:
            routes.append(Route(f"/api/v1{path}", endpoint, methods=list(methods)))

    # OpenAPI contract endpoints.  The whole app (including these two paths)
    # sits behind the central-deploy gateway's auth layer.
    routes.append(Route("/openapi.json", openapi_json_endpoint, methods=["GET"]))
    routes.append(Route("/docs", docs_endpoint, methods=["GET"]))

    # Chat-skill descriptor — served through a Starlette-native endpoint (see
    # routes/chat_skill.py) because the shared robotsix-http factory
    # (robotsix_http.fastapi.create_chat_skill_router) is FastAPI-only by
    # design: it mints a FastAPI APIRouter that a plain Starlette app cannot
    # host, so migrating /chat-skill to the factory is infeasible for this
    # server.  Frontmatter validity and route parity are still enforced
    # against the factory by the chat_skill_route_parity test.  It
    # intentionally lives outside _API_ROUTE_SPECS: no /api/v1 alias, and no
    # entry in the OpenAPI schema assembled above.
    routes.append(Route("/chat-skill", chat_skill_endpoint, methods=["GET"]))

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
    app.state.max_images_per_message = max_images_per_message
    app.state.max_image_bytes = max_image_bytes
    app.state.allowed_image_media_types = (
        allowed_image_media_types
        if allowed_image_media_types is not None
        else list(DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES)
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
    _register_inflight_probes(app.state.message_coalescer, subsession_registry)
    if periodic_definitions:
        from robotsix_chat.periodic import (
            PERIODIC_OWNER,
            PERIODIC_SCHEDULER_PERSIST_PATH,
            PeriodicScheduler,
            format_failsafe_partial_report,
            prune_closed_periodic_sessions,
        )
        from robotsix_chat.subsessions import SubsessionKind

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
                frame_type, payload = await queue.get()
                if frame_type == SSE_DONE_TYPE:
                    break
                if frame_type == SSE_ERROR_TYPE:
                    # A turn-level error is fanned out as an SSE_ERROR frame
                    # rather than raised by submit() (the coalescer spawns a
                    # background processor task that never re-raises), so
                    # distinguish it here. Raising makes the scheduler treat
                    # this run as failed — it is NOT completion-closed, and
                    # is left for the supersede-close safety net.
                    err_msg = (
                        payload.get("message") if isinstance(payload, dict) else None
                    )
                    raise RuntimeError(err_msg or "periodic turn failed")

        async def _periodic_close_previous(session_id: str) -> None:
            """Close a superseded periodic run through the UI close path."""
            from .routes.sessions import close_session_fully

            await close_session_fully(app, PERIODIC_OWNER, session_id)

        async def _periodic_close_completed(session_id: str) -> None:
            """Close a periodic run once its turn completes, via the UI close path."""
            from .routes.sessions import close_session_fully

            await close_session_fully(app, PERIODIC_OWNER, session_id)

        def _periodic_has_live_subsessions(session_id: str) -> bool:
            """Return True when a session still has a working subsession."""
            registry = app.state.subsession_registry
            if registry is None:
                return False
            # ``is_active`` is a @property (status in ACTIVE_STATUSES), not a
            # method — calling it would raise ``TypeError: 'bool' object is
            # not callable`` and leak an unhandled task exception, and the
            # completion-close gate would break for any session that ever
            # spawned a subsession.
            return any(sub.is_active for sub in registry.list_for_owner(session_id))

        async def _periodic_report_partial_result(
            session_id: str, error_message: str
        ) -> None:
            """Record a partial-failure report into a crashed run's transcript.

            When a periodic turn raises before completing, the normal submit
            path never records the exchange, leaving the session with no
            visible failure state. Appending the error here means future runs
            resume with full context of what was attempted and where it
            failed.
            """
            _p_store.record(
                session_id,
                PERIODIC_OWNER,
                "(periodic turn error)",
                error_message,
            )

        async def _periodic_ensure_partial_report(session_id: str) -> None:
            """Append a failsafe PARTIAL REPORT when a turn ended without one.

            A periodic turn can finish WITHOUT raising yet leave no report —
            token exhaustion or a provider cutoff can end the turn cleanly
            before the agent summarises its work. Conservative detection (low
            false-positive): append only when the transcript is empty or the
            final assistant reply is empty/whitespace-only. A healthy run ends
            with a non-empty assistant reply and is left untouched, so this
            never double-appends on a normal firing.
            """
            turns = _p_store.history(session_id)
            if turns and turns[-1][1].strip():
                return
            _p_store.record(
                session_id,
                PERIODIC_OWNER,
                "(periodic failsafe)",
                format_failsafe_partial_report(),
            )

        async def _periodic_carry_over(
            previous_session_id: str, session_id: str
        ) -> int:
            """Hand the previous run's live user_chat panels to the new run.

            Without this the supersede-close kills every decision panel the
            operator has not answered yet, and the new run — finding no open
            panel — spawns the same question again, every firing.
            """
            registry = app.state.subsession_registry
            if registry is None:
                return 0
            return int(
                registry.reassign_live_user_chats(previous_session_id, session_id)
            )

        async def _periodic_prune_closed(keep: set[str]) -> int:
            """Delete closed periodic runs outside the retention window."""
            return prune_closed_periodic_sessions(
                _p_store, keep, registry=app.state.subsession_registry
            )

        async def _periodic_report_pending_subsessions(session_id: str) -> None:
            """Surface OPEN user_chat panels when a periodic turn crashed.

            The agent normally surfaces open ``user_chat`` decision panels as
            its first operation, but a turn that fails before the agent runs
            never performs that check, so pending operator decisions stay
            invisible in the transcript. This fallback — invoked ONLY on the
            exception path — lists the run's live ``user_chat`` subsessions and
            records them directly, making the escalations visible independently
            of the main task succeeding. No-op when there are none open, so the
            transcript is not cluttered when nothing is pending.
            """
            registry = app.state.subsession_registry
            if registry is None:
                return
            pending = [
                sub
                for sub in registry.list_for_owner(session_id)
                if sub.kind is SubsessionKind.USER_CHAT and sub.is_active
            ]
            if not pending:
                return
            lines = [
                "PENDING OPERATOR DECISIONS (surfaced after a failed periodic "
                + "turn)",
                "",
                "The scheduled task failed before completing, but the "
                "following open user_chat decision panels are still awaiting an "
                "operator reply. They are live decisions, not deferred work:",
                "",
            ]
            for sub in pending:
                lines.append(f"- {sub.title} ({sub.id}) — status: {sub.status.value}")
                prompt = (sub.prompt or "").strip()
                if prompt:
                    lines.append(f"    Decision needed: {prompt}")
            _p_store.record(
                session_id,
                PERIODIC_OWNER,
                "(periodic pending subsessions)",
                "\n".join(lines),
            )

        app.state.periodic_scheduler = PeriodicScheduler(
            definitions=periodic_definitions,
            conversation_store=_p_store,
            submit_turn=_periodic_submit_turn,
            is_busy=_p_coalescer.is_busy,
            persist_path=periodic_state_path or PERIODIC_SCHEDULER_PERSIST_PATH,
            close_previous=_periodic_close_previous,
            close_completed=_periodic_close_completed,
            has_live_subsessions=_periodic_has_live_subsessions,
            report_partial_result=_periodic_report_partial_result,
            ensure_partial_report_on_interruption=_periodic_ensure_partial_report,
            report_pending_subsessions=_periodic_report_pending_subsessions,
            carry_over_subsessions=_periodic_carry_over,
            prune_closed_runs=_periodic_prune_closed,
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
    # The periodic summary scheduler is the single context-reduction
    # mechanism for ALL sessions (idle compaction removed); started during
    # the lifespan async phase.
    _eg = evergoing_settings or EvergoingSettings()
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

    if config_path is not None:
        app.state.config_path = config_path
    if draft_store_dir is not None:
        app.state.draft_store_dir = draft_store_dir
    return app
