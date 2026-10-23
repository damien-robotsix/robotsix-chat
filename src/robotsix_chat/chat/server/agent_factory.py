"""Chat agent construction from configuration.

``create_agent_from_settings`` builds the LLM agent with its full tool suite
from :class:`~robotsix_chat.config.Settings`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventSink
from robotsix_chat.component_access import build_component_access_tools
from robotsix_chat.component_client import build_component_tools
from robotsix_chat.config import Settings
from robotsix_chat.continuation import build_continuation_tools
from robotsix_chat.diagnostics import build_diagnostics_tools
from robotsix_chat.docker_digest import build_docker_digest_tools
from robotsix_chat.epic import build_decompose_epic_tool
from robotsix_chat.file_hub_tools import build_file_hub_tools
from robotsix_chat.gateway_route import build_gateway_route_tools
from robotsix_chat.http_probe import build_http_probe_tools
from robotsix_chat.knowledge import build_knowledge_tools
from robotsix_chat.langfuse import build_langfuse_inspect_tools
from robotsix_chat.lifecycle import build_lifecycle_tools
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.memory import ChatMemory, NullMemory, ReadOnlyMemory, build_memory
from robotsix_chat.public_fetch import build_public_fetch_tools
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.render_url import build_render_url_tools
from robotsix_chat.repo.actions import build_github_actions_tools
from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.security import build_github_security_tools
from robotsix_chat.repo.study import build_repo_study_tools
from robotsix_chat.selfreview import build_recent_activity_tools
from robotsix_chat.sftp import build_sftp_tools
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
)
from robotsix_chat.version_check import build_version_check_tools
from robotsix_chat.volume_tools import build_volume_tools

from .skill_wiring import _inject_skills, build_skill_tools

if TYPE_CHECKING:
    from robotsix_chat.subsessions import (
        CloseState,
        SubsessionContext,
        SubsessionEnv,
    )


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
        *build_diagnostics_tools(
            settings.diagnostics,
            store=diagnostic_store,
            direct_repo=settings.direct_repo,
        ),
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
        *build_file_hub_tools(
            settings.file_hub_tools,
            vision_model=settings.vision_model,
            vision_api_key=settings.openrouter_api_key.get_secret_value(),
        ),
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

    async def list_available_tools(query: str | None = None) -> str:
        """List all tools currently available to the agent.

        ``query`` optionally narrows the list to names containing any of the
        whitespace-separated terms (models send one by habit — a
        board-gates-drain turn was schema-rejected for it on 2026-09-08).

        Returns a sorted, newline-separated list of tool names.  Use this
        after a restart to verify that expected tools (e.g. newly-added
        tools from an image update) are present before declaring them
        ready.  If an expected tool is missing, the image may not have
        been updated — redeploy and verify via the lifecycle tools.

        Returns:
            A formatted list of available tool names.

        """
        names = [*_tool_names, "list_available_tools"]
        terms = [t.lower() for t in (query or "").split() if t.strip()]
        if terms:
            names = [n for n in names if any(t in n.lower() for t in terms)]
            if not names:
                return f"No tools match {query!r} (of {len(_tool_names) + 1})."
            header = (
                f"Tools matching {query!r} ({len(names)} of {len(_tool_names) + 1}):\n"
            )
        else:
            header = f"Available tools ({len(names)}):\n"
        return header + "\n".join(f"  - {name}" for name in names)

    raw_tools.append(list_available_tools)

    return [require_args(t) for t in raw_tools]


def _build_request_tools_factory(
    settings: Settings,
    subsession_env: SubsessionEnv | None,
    event_sink: EventSink | None,
    conversation_store: ConversationStore | None = None,
    configured_level: int | None = None,
) -> Callable[[str], list[Any]] | None:
    """Build a per-request tools factory for the main chat agent.

    Combines subsession tools (built per ``stream()`` call so closures
    capture the request's session id) with the escalation tools.
    Returns ``None`` when no per-request tools are configured.
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
) -> LlmioChatAgent:
    """Build an :class:`LlmioChatAgent` wired from *settings*.

    The backend is chosen by robotsix-llmio's capability level: *model_level*
    when given, else ``settings.chat_default_model_level``.  The level encodes the
    transport + model; ``settings.openrouter_api_key`` is forwarded only when the
    effective level's transport needs a key (keyless claudeSDK levels 2, 4, 5
    never receive one).

    When *settings* is ``None``, ``Settings.load()`` resolves configuration
    from the YAML config file and environment. When *instruction* is ``None``,
    it is taken from ``settings.agent_instruction``.

    *bare* (default ``False``) skips skill injection, feature tools,
    subsession wiring, and memory — the agent gets a ``NullMemory`` and no
    tools.  Use it for bounded text-transformation calls (e.g. the
    compaction summary agent).

    *memory_enabled* (default ``True``) gates only the full long-term memory
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
    # directive via ``USER_CHAT_FIRST_TURN_NOTE`` (subsessions/prompts.py) in
    # the worker.
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
    api_key = settings.openrouter_api_key.get_secret_value()

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
        # Build per-request tools factory — subsession and escalation tools
        # for the main agent.
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
        )

    # Read/write split for background agents. Recall is a retrieval-only
    # lookup (~0.4 s warm, no LLM call); cognify is a multi-minute LLM
    # pipeline that also contends with every concurrent recall. So an agent
    # whose WRITE gate is off can still safely READ — denying it the context
    # the main conversation already learned buys nothing.
    if bare:
        memory: ChatMemory = NullMemory()
    elif memory_enabled:
        memory = build_memory(settings.memory_component)
    elif settings.memory_component.enabled:
        memory = ReadOnlyMemory(build_memory(settings.memory_component))
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
    return agent
