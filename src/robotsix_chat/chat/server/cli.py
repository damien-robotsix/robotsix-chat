"""Chat SSE server — CLI entry point and server launcher.

``run_server_from_config`` is the console-script entry point: it loads
settings, configures logging, builds the agent, wires up all shared
state, and passes everything to ``run_server`` (which creates the ASGI
app via ``create_app`` and starts uvicorn).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.chat.summarize import SUMMARY_SYSTEM_PROMPT
from robotsix_chat.config import PROJECT_MAIN, Settings
from robotsix_chat.continuation.store import ContinuationStore
from robotsix_chat.diagnostics import DiagnosticStore
from robotsix_chat.knowledge.store import KnowledgeStore
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.startup_checks import check_component_connectivity

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
    compaction_keep_recent_turns: int = 2,
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
    periodic_definitions: Any = None,
    periodic_agent_factory: Any = None,
    periodic_state_path: str | None = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
    direct_repo_settings: Any = None,
    central_deploy_settings: Any = None,
    github_security_settings: Any = None,
    github_actions_settings: Any = None,
    mobile_auth: Any = None,
    config_path: str | None = None,
    diagnostic_store: Any = None,
    knowledge_store: Any = None,
    health_settings: Any = None,
    evergoing_settings: Any = None,
    continuation_store: Any = None,
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
        compaction_keep_recent_turns=compaction_keep_recent_turns,
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
        periodic_definitions=periodic_definitions,
        periodic_agent_factory=periodic_agent_factory,
        periodic_state_path=periodic_state_path,
        on_startup=on_startup,
        on_startup_async=on_startup_async,
        on_shutdown=on_shutdown,
        direct_repo_settings=direct_repo_settings,
        central_deploy_settings=central_deploy_settings,
        github_security_settings=github_security_settings,
        github_actions_settings=github_actions_settings,
        mobile_auth=mobile_auth,
        config_path=config_path,
        diagnostic_store=diagnostic_store,
        knowledge_store=knowledge_store,
        health_settings=health_settings,
        evergoing_settings=evergoing_settings,
        continuation_store=continuation_store,
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
    """Export main-agent Langfuse config to process env before SDK init.

    Reads the main agent's project (``PROJECT_MAIN``) out of the canonical
    ``langfuse`` credential block.  Uses direct assignment (not
    ``setdefault``) so config.json values always win.  Per the
    config-ownership standard, first-party credentials live in
    ``config/config.json``, never in the deploy plane.
    """
    creds = settings.langfuse.creds(PROJECT_MAIN)
    if creds.is_configured():
        os.environ["LANGFUSE_PUBLIC_KEY"] = creds.public_key.get_secret_value()
        os.environ["LANGFUSE_SECRET_KEY"] = creds.secret_key.get_secret_value()
        # llmio's setup_langfuse_tracing reads LANGFUSE_BASE_URL and falls back
        # to Langfuse Cloud US when it is absent; LANGFUSE_HOST is the langfuse
        # SDK / cognee name. Export both so every consumer sees the same host.
        os.environ["LANGFUSE_BASE_URL"] = settings.langfuse.host
        os.environ["LANGFUSE_HOST"] = settings.langfuse.host
    else:
        logger.info(
            "Langfuse project %r is not configured — main-agent tracing off",
            PROJECT_MAIN,
        )


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

    # -- llmio provider-failover configuration ----------------------------
    # Adopt the configured failover window up front so status reads show the
    # right policy before the first turn (each agent's tier config re-adopts
    # it per call as well).
    from robotsix_llmio.config.tier import FailoverConfig
    from robotsix_llmio.core.failover import get_failover_tracker

    get_failover_tracker().configure(
        FailoverConfig(window_seconds=settings.llmio_failover_window_seconds)
    )
    logger.info(
        "llmio provider-failover window configured: %.0f seconds",
        settings.llmio_failover_window_seconds,
    )
    if not settings.llmio_api_key.get_secret_value():
        logger.warning(
            "no llmio.api_key configured — provider failover to the keyed "
            "OpenRouter slot is unavailable; turns fail if the Claude "
            "subscription is exhausted or degraded"
        )

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
    from robotsix_chat.subsessions.worker import attach_slot_budget

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

    # Shared diagnostic store — created once so agent tools and the HTTP
    # endpoint share the same in-memory instance.  Events posted via the API
    # are immediately visible to agent tools without a restart.
    diagnostic_store = DiagnosticStore(settings.diagnostics.store_path)

    # Shared knowledge store — created once so the agent tools and the
    # session-lifecycle handlers (carryover persistence) use the same instance.
    knowledge_store = KnowledgeStore(settings.knowledge.path)

    # Continuation store — shared instance so the startup hook and the
    # agent tool read/write the same pending-continuation state.
    continuation_store = ContinuationStore(
        path=settings.continuation.store_path,
        max_consecutive=settings.continuation.max_consecutive,
    )

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
            # Background subsession workers run unattended; long-term cognee
            # memory is gated off by default (memory.subsession_enabled) so
            # they don't recall + cognify every turn around the clock.
            memory_enabled=s.memory.subsession_enabled,
            diagnostic_store=diagnostic_store,
            knowledge_store=knowledge_store,
        )

    env = SubsessionEnv(
        settings=settings,
        registry=subsession_registry,
        delivery=delivery,
        conversation_store=conversation_store,
        agent_factory=_subsession_agent_factory,
        event_sink=event_bus,
    )

    # Per-conversation slot-budget manager: governs how new monitors are
    # admitted when a conversation's monitor pool is at capacity (reuse a
    # paused slot, else queue — never evict a live monitor).  No-op when
    # disabled by config (monitor_slot_budget <= 0).
    attach_slot_budget(env)

    # -- periodic session agents -------------------------------------------
    # Periodic sessions run the exact same instruction and code path as an
    # operator session; the only per-preset knob is the model level. Agents
    # are cached per level so repeated firings reuse them. Long-term cognee
    # memory stays gated (memory.periodic_enabled) — these turns are
    # unattended and would otherwise cognify around the clock.
    _periodic_agents: dict[int | None, LlmioChatAgent] = {}

    def _periodic_agent_factory(model_level: int | None = None) -> LlmioChatAgent:
        if model_level not in _periodic_agents:
            _periodic_agents[model_level] = create_agent_from_settings(
                settings=settings,
                conversation_store=conversation_store,
                model_level=model_level
                if model_level is not None
                else settings.chat_model_level,
                subsession_env=env,
                event_sink=event_bus,
                memory_enabled=settings.memory.periodic_enabled,
                diagnostic_store=diagnostic_store,
                knowledge_store=knowledge_store,
            )
        return _periodic_agents[model_level]

    periodic_definitions = [d for d in settings.periodic.sessions if d.enabled]
    logger.info(
        "Periodic scheduler configured (%d enabled preset(s))",
        len(periodic_definitions),
    )

    if agent is None:
        agent = create_agent_from_settings(
            settings=settings,
            conversation_store=conversation_store,
            model_level=settings.chat_model_level,
            subsession_env=env,
            event_sink=event_bus,
            diagnostic_store=diagnostic_store,
            knowledge_store=knowledge_store,
            continuation_store=continuation_store,
        )
    # Wire the main agent into ParentDelivery now that both exist (see
    # ParentDelivery.set_agent for why this can't happen at construction
    # time) — main-chat-parent subsession outcomes then get a real reaction
    # turn instead of a passive history record.
    delivery.set_agent(agent)

    # Dedicated summariser agent for the idle-timeout compaction summary,
    # the carryover summary and conversation titles (bounded text
    # transformation, not open-ended reasoning).  It runs on its own
    # capability level and — crucially — its OWN system prompt: handed the
    # chat agent's prompt it behaved as the assistant and "continued" the
    # conversation (the 2026-08-30 compaction run echoed the last reply
    # verbatim: 65k chars in, 142 tokens out).  Every level is served by
    # the keyless default slot, so no key check is needed here.  bare=True:
    # a summary is a single bounded call over an explicit transcript — it
    # has no business paying for cross-session memory recall or agentic
    # tool access (ChatMemory.recall() alone was observed taking 90+
    # seconds in production, dwarfing the actual model call).
    summary_model_level = settings.summary_model_level
    summary_agent = create_agent_from_settings(
        instruction=SUMMARY_SYSTEM_PROMPT,
        settings=settings,
        conversation_store=conversation_store,
        model_level=summary_model_level,
        bare=True,
        diagnostic_store=diagnostic_store,
        knowledge_store=knowledge_store,
    )

    # -- feedback runner ---------------------------------------------------
    feedback_runner = None
    if settings.feedback.enabled:
        if not settings.feedback.board_url:
            logger.warning(
                "Feedback runner enabled but feedback.board_url is empty — "
                "all feedback runs will be skipped until a board URL is configured"
            )
        feedback_model_level = settings.feedback.model_level
        from robotsix_chat.feedback import FEEDBACK_SYSTEM_PROMPT, FeedbackRunner

        feedback_agent = create_agent_from_settings(
            instruction=FEEDBACK_SYSTEM_PROMPT,
            settings=settings,
            conversation_store=conversation_store,
            model_level=feedback_model_level,
            bare=True,
            diagnostic_store=diagnostic_store,
            knowledge_store=knowledge_store,
        )

        feedback_runner = FeedbackRunner(
            settings.feedback,
            feedback_agent,
            subsession_registry=subsession_registry,
            deploy_base_url=settings.central_deploy.url,
            deploy_api_key=settings.central_deploy.deploy_api_key.get_secret_value(),
        )
        logger.info("Feedback runner enabled (model_level=%d)", feedback_model_level)
    else:
        logger.info("Feedback runner disabled (feedback.enabled=false)")

    # -- resume persisted subsessions after redeploy -----------------------
    def _resume() -> None:
        """Resume periodic subsessions; report interrupted one-shot work."""
        resume_subsessions(env)

    # -- background watcher for paused monitors ---------------------------
    async def _start_watcher() -> None:
        """Launch the paused-monitor watcher as a background task."""
        from robotsix_chat.subsessions import watch_paused_monitors

        task = asyncio.create_task(watch_paused_monitors(env))
        env._tasks.add(task)
        task.add_done_callback(env._tasks.discard)
        logger.info("Paused-monitor watcher started.")

    # -- warm the memory backend off the request path ----------------------
    def _start_memory_warmup() -> None:
        """Fire cognee's cold start as a background task, if the backend wants one.

        Deliberately not awaited: warming imports cognee and opens the vector
        tables, which takes tens of seconds, and blocking here would delay
        readiness. Backends without a ``warm`` hook (NullMemory, ReadOnlyMemory
        over one) are simply skipped.
        """
        # Both hops are probed: ``ChatAgent`` does not declare ``memory``, and
        # not every backend behind it offers a warm-up.
        warm = getattr(getattr(agent, "memory", None), "warm", None)
        if warm is None:
            return
        task = asyncio.create_task(warm())
        env._tasks.add(task)
        task.add_done_callback(env._tasks.discard)
        logger.info("Memory warm-up started in the background.")

        # Kick off periodic LanceDB compaction/pruning (startup + interval).
        # Skipped cleanly by backends without the hook (NullMemory, ReadOnly).
        start_maintenance = getattr(
            getattr(agent, "memory", None), "start_maintenance", None
        )
        if start_maintenance is not None:
            try:
                start_maintenance()
            except Exception:
                logger.exception("Memory maintenance start failed — continuing")

    # -- async startup ------------------------------------------------------
    async def _startup_async() -> None:
        """Run the connectivity check, memory warm-up, watcher, continuation."""
        await check_component_connectivity(settings.central_deploy)
        _start_memory_warmup()
        # Start the paused-monitor watcher.
        await _start_watcher()
        # Fire any pending post-restart continuation.
        await _fire_continuation()

    async def _fire_continuation() -> None:
        """Check for a pending continuation and fire it if present.

        The continuation is one-shot (consumed on use) and guarded by
        ``max_consecutive``.  When a continuation fires, the stored prompt
        is injected into the target session as if the operator had sent it,
        and the agent's reply is recorded in the conversation history.

        Failures are logged but never crash startup — a continuation that
        cannot fire (e.g. the session was deleted) is silently dropped.
        """
        if not settings.continuation.enabled:
            return

        sid, prompt = continuation_store.consume_pending()
        if sid is None:
            return

        logger.info(
            "Firing continuation: session_id=%s prompt_preview=%r",
            sid,
            prompt[:80] if prompt else "",
        )

        # Fire as a background task so it does not block startup readiness.
        async def _run() -> None:
            try:
                # Ensure the session exists in the store and resolve its owner.
                store_session = conversation_store.get_session(sid)
                if store_session is None:
                    logger.warning(
                        "Continuation target session %s not found — dropping",
                        sid,
                    )
                    return
                owner_id = conversation_store.owner_for_session(sid) or "operator"
                # prompt is guaranteed non-None when sid is non-None.
                if prompt is None:
                    logger.warning("Continuation prompt is None — dropping")
                    return
                async with run_serializer.for_owner(owner_id):
                    reply_parts: list[str] = []
                    async for token in agent.stream(
                        prompt,
                        history=[],
                        session_id=sid,
                        client_id=sid,
                        trace_name="continuation",
                    ):
                        reply_parts.append(token)
                    full_reply = "".join(reply_parts)
                    conversation_store.record(sid, owner_id, prompt, full_reply)
                    logger.info(
                        "Continuation fired successfully: session_id=%s reply_chars=%d",
                        sid,
                        len(full_reply),
                    )
            except asyncio.CancelledError:
                logger.debug("Continuation task cancelled for session %s", sid)
            except Exception:
                logger.exception("Continuation failed for session %s — dropping", sid)

        task = asyncio.create_task(_run())
        env._tasks.add(task)
        task.add_done_callback(env._tasks.discard)

    # -- flush pending traces on shutdown ----------------------------------
    async def _flush_traces() -> None:
        """Force-flush any buffered Langfuse spans before the process exits.

        The OTel batch span processor exports on a timer (default ~30 s);
        pending spans are lost when the process exits before the next tick.
        This hook drains the buffer so interactive traces (including the
        agent's observation tree) are captured even when the server stops
        soon after a trace completes.
        """
        stop_maintenance = getattr(
            getattr(agent, "memory", None), "stop_maintenance", None
        )
        if stop_maintenance is not None:
            try:
                await stop_maintenance()
            except Exception:
                logger.exception("Memory maintenance stop failed — continuing")
        # Checkpoint and close the ladybug graph store so an interrupted WAL
        # write (a suspected corruption trigger across deploys) is avoided.
        shutdown_memory = getattr(getattr(agent, "memory", None), "shutdown", None)
        if shutdown_memory is not None:
            try:
                await shutdown_memory()
            except Exception:
                logger.exception("Memory graph-store shutdown failed — continuing")
        try:
            from robotsix_llmio.core.tracing import flush_tracing
        except ImportError:
            return
        flush_tracing()

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
        compaction_keep_recent_turns=settings.compaction_keep_recent_turns,
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
        periodic_definitions=periodic_definitions,
        periodic_agent_factory=_periodic_agent_factory,
        on_startup=_resume,
        on_startup_async=_startup_async,
        on_shutdown=_flush_traces,
        direct_repo_settings=settings.direct_repo,
        central_deploy_settings=settings.central_deploy,
        github_security_settings=settings.github_security,
        github_actions_settings=settings.github_actions,
        mobile_auth=settings.mobile_auth,
        diagnostic_store=diagnostic_store,
        knowledge_store=knowledge_store,
        health_settings=settings.health,
        evergoing_settings=settings.evergoing,
        continuation_store=continuation_store,
    )
