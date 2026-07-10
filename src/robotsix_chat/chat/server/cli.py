"""Chat SSE server — CLI entry point and server launcher.

``run_server_from_config`` is the console-script entry point: it loads
settings, configures logging, builds the agent, wires up all shared
state, and passes everything to ``run_server`` (which creates the ASGI
app via ``create_app`` and starts uvicorn).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.config import Settings
from robotsix_chat.config.constants import level_needs_api_key
from robotsix_chat.llm import LlmioChatAgent

from .app import create_agent_from_settings, create_app
from .routes import ChatAgent, RunSerializer

logger = logging.getLogger(__name__)


# Keyword parameters shared with create_app() are tracked in
# robotsix_chat.chat.server.app.SHARED_PARAMS — keep the two
# signatures in sync or the test suite will catch the drift.
def run_server(
    agent: ChatAgent,
    *,
    summary_agent: ChatAgent | None = None,
    host: str = "0.0.0.0",  # noqa: S104  # nosec B104
    port: int = 8000,
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
    subsession_registry: Any = None,
    subsession_delivery: Any = None,
    feedback_runner: Any = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
    direct_repo_settings: Any = None,
    github_security_settings: Any = None,
) -> None:
    """Start the chat SSE server on ``host:port``.

    Blocks until the process is interrupted (uvicorn handles
    ``SIGINT`` / ``SIGTERM``).
    """
    import uvicorn

    app = create_app(
        agent,
        summary_agent=summary_agent,
        serve_ui=serve_ui,
        idle_timeout_minutes=idle_timeout_minutes,
        compaction_min_turns=compaction_min_turns,
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
        feedback_runner=feedback_runner,
        on_startup=on_startup,
        on_startup_async=on_startup_async,
        on_shutdown=on_shutdown,
        direct_repo_settings=direct_repo_settings,
        github_security_settings=github_security_settings,
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
        # llmio's setup_langfuse_tracing reads LANGFUSE_BASE_URL and falls back
        # to Langfuse Cloud US when it is absent; LANGFUSE_HOST is the langfuse
        # SDK / cognee name. Export both so every consumer sees the same host.
        os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse.host)
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse.host)


def _configure_logging(settings: Settings) -> None:
    """Wire Python stdlib logging through structlog.

    Uses ``structlog.stdlib.ProcessorFormatter`` as a bridge so existing
    ``logging.getLogger(__name__).info(...)`` calls continue to work while
    all output flows through the configured processor chain.  JSON output
    is used when *settings.log_json_format* is ``True`` (the default);
    human-readable console output when ``False``.

    Uvicorn's loggers are cleared and set to propagate so access logs
    flow through the same structured pipeline.
    """
    import structlog

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if settings.log_json_format:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level.upper())

    # Let Uvicorn loggers propagate through the same pipeline.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


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
    _configure_logging(settings)

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
        event_sink=event_bus,
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
            event_sink=event_bus,
        )
    # Wire the main agent into ParentDelivery now that both exist (see
    # ParentDelivery.set_agent for why this can't happen at construction
    # time) — main-chat-parent subsession outcomes then get a real reaction
    # turn instead of a passive history record.
    delivery.set_agent(agent)

    # Cheap dedicated agent for POST /summary (bounded extraction, not
    # open-ended reasoning) — avoids running the main agent's often-pricier
    # level on every single turn just to regenerate the summary. Unlike
    # llmio_model_level, a missing key for this level is not fatal: fall
    # back to the keyless tier (3) so a deployment without an OpenRouter
    # key still starts. bare=True: the summary is a single bounded
    # text-transformation call over an explicit transcript — it has no
    # business paying for cross-session memory recall or agentic tool
    # access (ChatMemory.recall() alone was observed taking 90+ seconds in
    # production, dwarfing the actual model call).
    summary_model_level = settings.summary_model_level
    if (
        level_needs_api_key(summary_model_level)
        and not settings.llmio_api_key.get_secret_value()
    ):
        logger.warning(
            "summary_model_level=%d needs an OpenRouter API key which is not "
            "configured — falling back to level 3 for POST /summary",
            summary_model_level,
        )
        summary_model_level = 3
    summary_agent = create_agent_from_settings(
        settings=settings,
        conversation_store=conversation_store,
        model_level=summary_model_level,
        bare=True,
    )

    # -- feedback runner ---------------------------------------------------
    feedback_runner = None
    if settings.feedback.enabled:
        feedback_model_level = settings.feedback.model_level
        if (
            level_needs_api_key(feedback_model_level)
            and not settings.llmio_api_key.get_secret_value()
        ):
            logger.warning(
                "feedback.model_level=%d needs an OpenRouter API key which is "
                "not configured — falling back to level 3 for feedback runner",
                feedback_model_level,
            )
            feedback_model_level = 3
        from robotsix_chat.feedback import FEEDBACK_SYSTEM_PROMPT, FeedbackRunner

        feedback_agent = create_agent_from_settings(
            instruction=FEEDBACK_SYSTEM_PROMPT,
            settings=settings,
            conversation_store=conversation_store,
            model_level=feedback_model_level,
            bare=True,
        )

        feedback_runner = FeedbackRunner(
            settings.feedback,
            feedback_agent,
            subsession_registry=subsession_registry,
        )
        logger.info("Feedback runner enabled (model_level=%d)", feedback_model_level)

    # -- resume persisted subsessions after redeploy -----------------------
    def _resume() -> None:
        """Resume periodic subsessions; report interrupted one-shot work."""
        resume_subsessions(env)

    logger.info(
        "Resolved persistence paths: conversation=%s, knowledge=%s, "
        "memory_data=%s, diagnostics=%s, subsessions=%s",
        settings.conversation.persist_path,
        settings.knowledge.path,
        settings.memory.data_dir,
        settings.diagnostics.store_path,
        settings.subsessions.store_path,
    )

    _run_server(
        agent,
        summary_agent=summary_agent,
        host=settings.server_host,
        port=settings.server_port,
        idle_timeout_minutes=settings.idle_timeout_minutes,
        compaction_min_turns=settings.compaction_min_turns,
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
        feedback_runner=feedback_runner,
        on_startup=_resume,
        direct_repo_settings=settings.direct_repo,
        github_security_settings=settings.github_security,
    )
