"""Chat SSE server — CLI entry point and server launcher.

``run_server_from_config`` is the console-script entry point: it loads
settings, configures logging, builds the agent, wires up all shared
state, and passes everything to ``run_server`` (which creates the ASGI
app via ``create_app`` and starts uvicorn).
"""

from __future__ import annotations

import importlib.util
import logging
import logging.config
from collections.abc import Callable
from pathlib import Path
from typing import Any

from robotsix_chat.chat.auth import BasicAuthConfig
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.chat.tasks import TaskRegistry
from robotsix_chat.config import Settings
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.pending_questions.store import PendingQuestionsStore

from .app import create_agent_from_settings, create_app
from .routes import ChatAgent, RunSerializer

logger = logging.getLogger(__name__)


def run_server(
    agent: ChatAgent,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
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
    check_loop_registry: Any = None,
    run_serializer: RunSerializer | None = None,
    pq_store: PendingQuestionsStore | None = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> None:
    """Start the chat SSE server on ``host:port``.

    Blocks until the process is interrupted (uvicorn handles
    ``SIGINT`` / ``SIGTERM``).
    """
    import uvicorn

    app = create_app(
        agent,
        serve_ui=serve_ui,
        idle_timeout_minutes=idle_timeout_minutes,
        max_images_per_message=max_images_per_message,
        max_image_bytes=max_image_bytes,
        allowed_image_media_types=allowed_image_media_types,
        cors_allow_origins=cors_allow_origins,
        auth=auth,
        correlation_id_header=correlation_id_header,
        conversation_store=conversation_store,
        task_registry=task_registry,
        event_bus=event_bus,
        check_loop_registry=check_loop_registry,
        run_serializer=run_serializer,
        pq_store=pq_store,
        on_startup=on_startup,
        on_startup_async=on_startup_async,
        on_shutdown=on_shutdown,
    )
    uvicorn.run(app, host=host, port=port)


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
    # Lazy import so tests can patch ``robotsix_chat.chat.server.run_server``
    # and the patch is visible through the package re-export.
    from . import run_server as _run_server

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

    # -- shared task registry + delivery channel for delegation -----------
    # Two independent notification sinks for background-task lifecycles:
    #
    # 1. EventBus → GET /events SSE → browser UI
    #    TaskRegistry.complete() / .fail() publish task_completed / task_failed
    #    frames to the EventBus, which the /events endpoint streams to the
    #    connected browser.  This path is unchanged.
    #
    # 2. ConversationDeliveryChannel → ConversationStore → agent history
    #    The runner's _worker also calls channel.publish(session_id, frame).
    #    This channel records completed/failed task results into the
    #    ConversationStore keyed by the originating session_id, so the
    #    foreground agent sees them in its next-turn history and can relay
    #    task IDs / URLs / findings to the user.
    #
    # The two sinks have different destinations (browser SSE vs. agent
    # history) — there is no duplicate-frame concern.
    #
    # The conversation store MUST be constructed before the channel so both
    # the channel and run_server() receive the exact same store instance.
    from robotsix_chat.chat.delegation import ConversationDeliveryChannel
    from robotsix_chat.chat.loops import CheckLoopRegistry, resume_check_loops

    event_bus = EventBus()
    registry = TaskRegistry(event_sink=event_bus)
    check_loop_registry = CheckLoopRegistry(event_sink=event_bus)

    persist_path_str = settings.conversation.persist_path
    conversation_store = ConversationStore(
        idle_reset_seconds=settings.conversation.idle_reset_seconds,
        max_history_turns=settings.conversation.max_history_turns,
        max_conversations=settings.conversation.max_conversations,
        persist_path=Path(persist_path_str) if persist_path_str else None,
    )

    # Per-owner run serializer — shared between /chat requests and
    # tick-triggered runs so they never overlap for the same owner.
    run_serializer = RunSerializer()

    # No-loop-tools foreground agent factory for tick-triggered runs.
    # Omits check_loop_registry so a tick-triggered run can never spawn
    # a new check loop (preserves the loop-sub-agent no-loop-tools boundary).
    # Other foreground tools (delegation, consult_mill, etc.) remain.
    def _tick_agent_factory(s: Settings) -> LlmioChatAgent:
        return create_agent_from_settings(
            settings=s,
            task_registry=registry,
            delivery_channel=channel,
            check_loop_registry=None,  # NO loop tools — recursion guard
            conversation_store=conversation_store,
        )

    # The channel uses the no-loop-tools factory for tick-triggered runs;
    # we build the channel first, then wire it as the delivery_channel for
    # the foreground agent (which DOES get loop tools).
    channel = ConversationDeliveryChannel(
        conversation_store,
        event_bus=event_bus,
        run_serializer=run_serializer,
        agent_factory=_tick_agent_factory,
        settings=settings,
    )

    # Shared pending-questions store wired to the event bus so the
    # frontend panel receives real-time SSE updates when the agent
    # adds / updates / removes entries.
    pq_store = PendingQuestionsStore(event_bus=event_bus)

    if agent is None:
        agent = create_agent_from_settings(
            settings=settings,
            task_registry=registry,
            delivery_channel=channel,
            check_loop_registry=check_loop_registry,
            conversation_store=conversation_store,
            pq_store=pq_store,
        )

    # -- resume persisted check loops after redeploy ----------------------
    def _resume() -> None:
        """Resume any check loops that were RUNNING at last shutdown."""
        resume_check_loops(check_loop_registry, settings, channel=channel)

    # -- component-agent responder (disabled by default; gated on the -----
    # -- optional broker extra) -------------------------------------------
    async def _start_responder() -> None:
        """Start the component-agent responder when enabled + broker present."""
        if not settings.component_agent.enabled:
            return
        try:
            found = importlib.util.find_spec("robotsix_agent_comm")
        except (ValueError, ModuleNotFoundError):
            found = None
        if not found:
            logger.info(
                "component_agent.enabled=True but the broker extra "
                "(robotsix-agent-comm) is not installed — responder not started."
            )
            return
        from robotsix_chat.component_agent.responder import (
            ComponentAgentResponder,
        )

        _responder = ComponentAgentResponder(
            settings,
            check_loop_registry=check_loop_registry,
            conversation_store=conversation_store,
            event_bus=event_bus,
        )
        # Stash on the function so _stop_responder can reach it.
        _start_responder._responder = _responder  # type: ignore[attr-defined]
        await _responder.start()

    async def _stop_responder() -> None:
        """Stop the component-agent responder if it was started."""
        responder = getattr(_start_responder, "_responder", None)
        if responder is not None:
            await responder.stop()

    auth = (
        BasicAuthConfig(
            username=settings.auth.username, password=settings.auth.password
        )
        if settings.auth.enabled
        else None
    )
    _run_server(
        agent,
        host=settings.server_host,
        port=settings.server_port,
        idle_timeout_minutes=settings.idle_timeout_minutes,
        max_images_per_message=settings.max_images_per_message,
        max_image_bytes=settings.max_image_bytes,
        allowed_image_media_types=settings.allowed_image_media_types,
        cors_allow_origins=settings.cors_allow_origins,
        auth=auth,
        correlation_id_header=settings.correlation_id_header,
        conversation_store=conversation_store,
        task_registry=registry,
        event_bus=event_bus,
        check_loop_registry=check_loop_registry,
        run_serializer=run_serializer,
        pq_store=pq_store,
        on_startup=_resume,
        on_startup_async=_start_responder,
        on_shutdown=_stop_responder,
    )
