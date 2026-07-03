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
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.config import Settings
from robotsix_chat.llm import LlmioChatAgent

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
    correlation_id_header: str = "X-Request-ID",
    conversation_store: ConversationStore | None = None,
    event_bus: EventBus | None = None,
    run_serializer: RunSerializer | None = None,
    subsession_registry: Any = None,
    subsession_delivery: Any = None,
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
        correlation_id_header=correlation_id_header,
        conversation_store=conversation_store,
        event_bus=event_bus,
        run_serializer=run_serializer,
        subsession_registry=subsession_registry,
        subsession_delivery=subsession_delivery,
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


def _export_langfuse_env(settings: Settings) -> None:
    """Export main-agent Langfuse config to process env before SDK init."""
    pk = settings.langfuse.public_key.get_secret_value()
    if pk:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", pk)
        os.environ.setdefault(
            "LANGFUSE_SECRET_KEY",
            settings.langfuse.secret_key.get_secret_value(),
        )
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse.host)


def run_server_from_config(agent: ChatAgent | None = None) -> None:
    """Start the chat SSE server using ``Settings.load()`` for configuration.

    Resolves settings through the full cascade (pydantic defaults → YAML
    config file → environment, with ``.env`` support), configures Python
    logging, builds a default :class:`LlmioChatAgent` when *agent* is
    ``None`` (using ``agent_instruction`` from config), and delegates to
    :func:`run_server`. (Authentication is centralized at the central-deploy
    gateway — the server itself is unauthenticated.)
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
    _export_langfuse_env(settings)
    _setup_observability()

    # -- unified subsession system -----------------------------------------
    # One registry owns every subsession (task / periodic / user_chat, all
    # nesting depths).  Lifecycle frames go to the EventBus → GET /events →
    # browser; terminal summaries go through ParentDelivery → either the
    # owning chat session's history (parent = main chat) or the parent
    # subsession's inbox (nested).
    from robotsix_chat.subsessions import (
        CloseState,
        ParentDelivery,
        SubsessionContext,
        SubsessionEnv,
        SubsessionRegistry,
        resume_subsessions,
    )

    event_bus = EventBus()

    persist_path_str = settings.conversation.persist_path
    conversation_store = ConversationStore(
        idle_reset_seconds=settings.conversation.idle_reset_seconds,
        max_history_turns=settings.conversation.max_history_turns,
        max_conversations=settings.conversation.max_conversations,
        persist_path=Path(persist_path_str) if persist_path_str else None,
    )

    # Per-owner run serializer — shared between /chat requests and
    # subsession summary writes so they never overlap for the same owner.
    run_serializer = RunSerializer()

    subsession_registry = SubsessionRegistry(
        event_sink=event_bus,
        store_path=Path(settings.subsessions.store_path),
        transcript_max_entries=settings.subsessions.transcript_max_entries,
    )
    delivery = ParentDelivery(
        conversation_store=conversation_store,
        registry=subsession_registry,
        run_serializer=run_serializer,
    )

    # Subsession agent factory: same full tool suite as the main agent,
    # plus the depth-aware subsession tools bound to the worker's context.
    # `env` is created right after (late binding through the closure).
    def _subsession_agent_factory(
        s: Settings,
        model_level: int,
        ctx: SubsessionContext,
        close_state: CloseState,
    ) -> LlmioChatAgent:
        return create_agent_from_settings(
            settings=s,
            conversation_store=conversation_store,
            model_level=model_level,
            subsession_env=env,
            subsession_ctx=ctx,
            subsession_close_state=close_state,
        )

    env = SubsessionEnv(
        settings=settings,
        registry=subsession_registry,
        delivery=delivery,
        conversation_store=conversation_store,
        agent_factory=_subsession_agent_factory,
        event_sink=event_bus,
    )

    if agent is None:
        agent = create_agent_from_settings(
            settings=settings,
            conversation_store=conversation_store,
            subsession_env=env,
        )

    # -- resume persisted subsessions after redeploy -----------------------
    def _resume() -> None:
        """Resume periodic subsessions; report interrupted one-shot work."""
        resume_subsessions(env)

    # -- component-agent responder (disabled by default; gated on the -----
    # -- optional broker extra) -------------------------------------------
    async def _start_responder() -> None:
        """Start the component-agent responder when enabled + broker present."""
        if not settings.component_agent.enabled:
            return
        try:
            found = importlib.util.find_spec("robotsix_agent_comm")
        except ValueError, ModuleNotFoundError:
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
            subsession_registry=subsession_registry,
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

    _run_server(
        agent,
        host=settings.server_host,
        port=settings.server_port,
        idle_timeout_minutes=settings.idle_timeout_minutes,
        max_images_per_message=settings.max_images_per_message,
        max_image_bytes=settings.max_image_bytes,
        allowed_image_media_types=settings.allowed_image_media_types,
        cors_allow_origins=settings.cors_allow_origins,
        correlation_id_header=settings.correlation_id_header,
        conversation_store=conversation_store,
        event_bus=event_bus,
        run_serializer=run_serializer,
        subsession_registry=subsession_registry,
        subsession_delivery=delivery,
        on_startup=_resume,
        on_startup_async=_start_responder,
        on_shutdown=_stop_responder,
    )
