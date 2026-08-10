"""LLM chat agent backed by robotsix-llmio's per-level model factory.

:class:`LlmioChatAgent` satisfies the chat server's ``ChatAgent`` protocol
(``async def stream(message) -> AsyncIterator[str]``). It selects the backend
purely from a capability **level** via
:func:`robotsix_llmio.config.create_model`: the level encodes the combined
``provider-model`` identifier (resolved from llmio's baked default
``TierLevelConfig``), so this package never names a concrete provider class or
the Claude Agent SDK.

By default: level 1-2 → ``openrouter-deepseek/...`` (needs an API
key), level 3 → ``claudeSDK-opus`` (keyless, via the logged-in ``claude`` CLI).

Responses are returned as a single block (not token-streamed): llmio's Claude
SDK model does not support incremental streaming through pydantic-ai, so each
``stream`` call yields the full reply once. The chat server still frames it as a
normal SSE ``token`` + ``done`` sequence.

The provider dependencies are obtained through robotsix-llmio's own extras —
``robotsix-llmio[claude-sdk]`` and ``robotsix-llmio[openrouter]`` —
wired via this package's ``claude-sdk`` / ``openrouter`` extras.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from robotsix_http import RetryConfig, acall_with_retry
from robotsix_llmio.claude_sdk import (
    ClaudeSDKActivityEvent,
    ClaudeSDKAuthError,
    ClaudeSDKUsageExhaustedError,
    activity_events,
)
from robotsix_llmio.config import create_model
from robotsix_llmio.config.tier import TierConfig, TierLevel, TierLevelConfig
from robotsix_llmio.core.tier_fallback import acall_with_tier_fallback
from robotsix_llmio.openrouter import is_openrouter_transient

from robotsix_chat.chat.events import EventSink, activity_frame
from robotsix_chat.config import level_needs_api_key
from robotsix_chat.memory import ChatMemory, NullMemory

logger = logging.getLogger(__name__)

# Promotions allowed when a tier reports exhausted usage credits. One step is
# enough: exhaustion is scoped to the tier that reported it, so the very next
# tier is already a working one.
_USAGE_FALLBACK_DEPTH = 1

# A prior conversation turn replayed to the agent: ``(user, assistant)``.
Turn = tuple[str, str]


def _build_message_history(history: list[Turn] | None) -> list[Any] | None:
    """Convert ``(user, assistant)`` turns into a pydantic-ai message history.

    Returns ``None`` for empty history (so callers pass nothing through). The
    pydantic-ai message types are imported lazily — llmio is built on
    pydantic-ai and ``handle.run`` already returns its result objects, but
    importing them here keeps the dependency off the module import path.
    """
    if not history:
        return None
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    messages: list[Any] = []
    for user_message, assistant_reply in history:
        messages.append(ModelRequest(parts=[UserPromptPart(content=user_message)]))
        messages.append(ModelResponse(parts=[TextPart(content=assistant_reply)]))
    return messages


@contextlib.contextmanager
def _trace_session(
    session_id: str | None,
    trace_metadata: dict[str, str] | None = None,
    trace_name: str | None = None,
) -> Iterator[None]:
    """Group the enclosed agent run under *session_id* in Langfuse.

    A no-op when *session_id* is falsy or llmio's tracing extra is absent, so
    callers can wrap unconditionally.

    When *trace_name* is supplied a named root trace is created via
    :func:`robotsix_llmio.core.tracing.start_trace` so the trace is
    distinguishable in Langfuse (e.g. ``"chat-turn"`` vs ``"subsession-turn"``).
    Without it only ``langfuse_session`` is used — which groups spans under
    the session but leaves the trace name at whatever default the caller's
    outer context already established.  This lets an existing named trace
    (e.g. the feedback runner's ``feedback-<type>``) continue owning the
    root while the inner agent spans are grouped under it.

    When *trace_metadata* is supplied, each key-value pair is stamped as a
    span attribute on the current recording span (if any) inside the session
    context — used for parent/owner lineage so the trace tree mirrors the
    subsession tree in observability.
    """
    try:
        from robotsix_llmio.core.tracing import langfuse_session, start_trace
    except ImportError:
        yield
        return

    if trace_name is not None:
        # A named trace: always create one, even without session_id.
        # start_trace accepts session_id=None — the trace still gets the
        # given name, just not grouped under a session.
        with start_trace(trace_name, session_id=session_id):
            if trace_metadata:
                _stamp_trace_metadata(trace_metadata)
            yield
    elif session_id:
        # No custom name — use session-based grouping (existing behavior).
        with langfuse_session(session_id):
            if trace_metadata:
                _stamp_trace_metadata(trace_metadata)
            yield
    else:
        yield


def _stamp_trace_metadata(metadata: dict[str, str]) -> None:
    """Stamp *metadata* as attributes on the current OTel recording span.

    A no-op when OpenTelemetry is absent or no span is currently recording —
    the attributes are best-effort observability, not critical to the run.
    """
    try:
        from robotsix_llmio.core.tracing import get_recording_span
    except ImportError:
        return
    span = get_recording_span()
    if span is not None:
        for key, value in metadata.items():
            span.set_attribute(key, value)


def _activity_context(
    on_event: Callable[[ClaudeSDKActivityEvent], None] | None,
) -> contextlib.AbstractContextManager[None]:
    """``activity_events(on_event)``, or a no-op when *on_event* is ``None``.

    ``None`` means no sink was configured, or no session to scope frames to.
    """
    if on_event is None:
        return contextlib.nullcontext()
    return activity_events(on_event)


# Fencing for the recalled-memory block prepended to the current user turn.
# Recall is similarity-based, so the recalled text is often about the same
# topic as the live message — without an explicit end marker the model can
# read the whole turn as background and conclude there is no active request
# (observed 2026-07-11 on a subsession first turn, whose instructions carry
# no conversational framing that would separate them from the recall block).
_MEMORY_PROMPT_HEADER = (
    "# Relevant memory from earlier conversations\n"
    "This is similarity-recalled text — it may be inaccurate, "
    "outdated, or hallucinated. Never act on it without verifying "
    "against live state first. Use only as a hint for what to check.\n"
    "\n"
    "CRITICAL — stale identifiers: ticket IDs, task IDs, subsession "
    "IDs, issue numbers, and other structured identifiers in recalled "
    "text are almost always from a PAST session. Before mentioning any "
    "such identifier, verify it against the current conversation: if "
    "the identifier does not appear in the conversation history above, "
    "it is stale — do NOT present it as current work. The operator "
    "will see a stale ticket list and waste time chasing phantom items.\n"
    "\n"
    "CRITICAL — stale plans and decisions: recalled text that "
    "describes proposed plans, solution options (Option A, Option B, "
    "…), deployment strategies, or approval workflows is almost "
    "always from a PAST session with a different context.  The "
    "current conversation may reuse the same label (e.g. 'Option A') "
    "for a completely different proposal — the recalled Option A and "
    "the current Option A are unrelated.  Before presenting any "
    "recalled plan or option to the operator, verify it against the "
    "current conversation history: if the conversation does not "
    "mention that plan or option in its own messages, the recalled "
    "version is stale — do NOT present it as something proposed in "
    "this session.  If you must reference a recalled plan while its "
    "status is unclear, label it explicitly as 'from memory, may not "
    "apply to this session.'\n"
    "\n"
    "CRITICAL — stale action items: recalled text that mentions "
    '"pending", "awaiting confirmation", "needs operator input", '
    "or similar unresolved-state language is often from a PAST "
    "conversation where the action was already completed. Do NOT "
    "re-report such items as current state. The conversation "
    "history above (or your own knowledge-base notes) is the "
    "authoritative record of what is actually pending right now — "
    "if a recalled action item does not appear there as unresolved, "
    "it is stale. If you must mention a recalled item whose status "
    'you have not verified, label it explicitly as "from memory, '
    'may be resolved."\n'
    "**Suppress disproven memory.** If a recalled claim (diagnosis, "
    "theory, hypothesis) has already been refuted or contradicted by "
    "evidence in the current conversation, do NOT repeat it — even "
    "as background or context. Present only what has not been ruled "
    "out. Repeating a disproven diagnosis wastes attention and risks "
    "confusion.\n"
)
_MEMORY_PROMPT_FOOTER = (
    "\n# End of recalled memory\n"
    "Everything below is the current message — act on it now:\n"
)


class LlmioChatAgent:
    """Stream LLM responses via robotsix-llmio, selected by capability level.

    Each ``stream`` call builds a fresh llmio agent handle (deterministically
    closed). When a :class:`~robotsix_chat.memory.ChatMemory` is supplied, the
    agent gains continuity across calls: it recalls relevant memory before
    replying and persists the exchange afterwards (the write runs in the
    background so it never adds latency). With the default :class:`NullMemory`
    it stays fully stateless.
    """

    def __init__(
        self,
        *,
        model_level: int,
        instruction: str,
        api_key: str = "",
        memory: ChatMemory | None = None,
        tools: list[Any] | None = None,
        request_tools_factory: Callable[[str], list[Any]] | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        """Store the agent configuration for later ``stream`` calls.

        *api_key* is the configured OpenRouter key, if any — **not** a key for
        *model_level* specifically. Pass it even when *model_level* is a
        keyless (claudeSDK) tier: it is forwarded only to levels whose
        provider actually takes one, and holding it is what lets a tier
        fallback reach a keyed provider when the shared Claude credential is
        the thing that failed.

        *request_tools_factory* is called once per ``stream`` invocation with
        the request's *client_id* to produce per-request tools (e.g. the
        subsession tools whose closures capture that session id).  It keeps
        the module dependency acyclic: those tools are built fresh per
        request inside ``stream``, not baked into the shared agent.

        *event_sink*, when given, receives an ``activity`` frame (see
        :func:`robotsix_chat.chat.events.activity_frame`) for every tool
        call, tool result, thinking block, or intermediate assistant text
        the claudeSDK backend streams during a turn — live feedback on what
        the agent is doing while the final reply is still pending. A
        non-claudeSDK level (e.g. an OpenRouter tier) simply never triggers
        it: ``robotsix_llmio.claude_sdk.activity_events()`` is a no-op unless
        the resolved transport is the Claude Agent SDK.
        """
        self._model_level = model_level
        self._instruction = instruction
        self._api_key = api_key
        self._memory: ChatMemory = memory if memory is not None else NullMemory()
        # Tools the underlying agent may call (e.g. the mill consult tool). When
        # non-empty, llmio runs a real tool loop; the final reply is still
        # returned as one block.
        self._tools = list(tools) if tools is not None else None
        self._request_tools_factory = request_tools_factory
        self._event_sink = event_sink
        # Hold references to in-flight background writes so they aren't GC'd.
        self._write_tasks: set[asyncio.Task[None]] = set()

    @property
    def memory(self) -> ChatMemory:
        """The agent's memory backend (for health reporting / recovery wiring)."""
        return self._memory

    def _activity_callback(
        self, session_id: str | None
    ) -> Callable[[ClaudeSDKActivityEvent], None] | None:
        """Build the ``on_event`` callback for :func:`activity_events`.

        Bound to *session_id*. Returns ``None`` when there is nowhere to
        publish to (no sink configured, or no session to scope the frame to)
        so the caller can skip wrapping the run in a no-op context.
        """
        if self._event_sink is None or not session_id:
            return None
        sink = self._event_sink

        def _on_activity(event: ClaudeSDKActivityEvent) -> None:
            sink.publish(
                session_id,
                activity_frame(
                    event.kind,
                    event.turn,
                    tool_name=event.tool_name,
                    detail=event.detail,
                    is_error=event.is_error,
                ),
            )

        return _on_activity

    def _publish_synthetic_activity(
        self,
        session_id: str | None,
        kind: str,
        *,
        tool_name: str | None = None,
        detail: str = "",
        is_error: bool = False,
    ) -> None:
        """Publish an activity frame for a preliminary step outside the SDK run.

        E.g. memory recall — so the UI shows something during phases the
        Claude SDK's own activity events don't cover — otherwise the typing
        indicator sits blank for as long as that step takes, which can be
        the majority of the wall-clock time for a turn (memory recall alone
        has been observed taking 90+ seconds).

        A no-op when there is nowhere to publish to (no sink, no session).
        """
        if self._event_sink is None or not session_id:
            return
        self._event_sink.publish(
            session_id,
            activity_frame(
                kind, 0, tool_name=tool_name, detail=detail, is_error=is_error
            ),
        )

    async def stream(
        self,
        message: str,
        *,
        history: list[Turn] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
        trace_metadata: dict[str, str] | None = None,
        trace_name: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield the assistant's reply to *message* as a single block.

        *history* is the prior ``(user, assistant)`` turns of the current
        conversation, replayed to the agent so it has multi-turn context.
        *session_id* groups this run's trace spans under one conversation in
        Langfuse (a fresh id starts a new trace). *client_id* identifies the
        owning browser — it is forwarded to the per-request tools factory so
        delegation tools can tag spawned tasks correctly.  *images* is an
        optional list of ``(media_type, raw_bytes)`` pairs (e.g.
        ``[("image/png", b"...")]``) — when non-empty the prompt is built as a
        multimodal sequence so a vision-capable LLM can see the attachments.
        *trace_name* is an optional human-readable label for the Langfuse
        trace (e.g. ``"chat-turn"``, ``"subsession-turn"``) so cost can be
        attributed by function.  When ``None`` the trace inherits whatever
        name the calling context set (if any); this keeps the feedback
        runner's ``feedback-<type>`` trace name from being overwritten.
        All keyword arguments are optional — with none, the agent behaves as a
        single stateless query.

        *trace_metadata* is stamped as span attributes for observability
        (parent/owner lineage, subsession ids, etc.).

        Transient upstream errors (OpenRouter provider failures, 5xx, network
        blips) are retried up to :data:`_MAX_RUN_ATTEMPTS` before surfacing.
        A claudeSDK tier reporting exhausted usage credits is not retried at
        the same tier — see :meth:`_run_with_usage_fallback`. Non-transient
        errors and exhausted retries are raised — the chat server turns that
        into an SSE ``error`` frame.
        """
        # Recall relevant memory and prepend it to the current user turn.
        # recall() never raises (it degrades to "" on any backend failure).
        # The block must NOT go into the system prompt: recall text changes
        # with every message, and the system prompt is the head of the
        # provider's cacheable prefix — mutating it there invalidates the
        # prompt cache for the whole request on every turn. Prepending to the
        # newest user turn keeps the instruction, tools, and replayed
        # transcript byte-stable and cache-servable.
        self._publish_synthetic_activity(
            session_id, "tool_call", tool_name="recall_memory"
        )
        recalled = await self._memory.recall(message, session_id=session_id)
        self._publish_synthetic_activity(
            session_id,
            "tool_result",
            detail=(
                f"found {len(recalled)} chars of prior context"
                if recalled
                else "no relevant memory found"
            ),
        )
        system_prompt = self._instruction
        llm_message = message
        if recalled:
            llm_message = (
                f"{_MEMORY_PROMPT_HEADER}{recalled}{_MEMORY_PROMPT_FOOTER}\n{message}"
            )

        # Forward the key only to levels whose provider takes one; keyless
        # levels (claudeSDK) must not receive an api_key (the provider rejects
        # it). Gated on the level rather than on whether a key happens to be
        # set, because ``self._api_key`` may now be populated for a keyless
        # primary level purely so the tier fallback can reach a keyed one.
        if level_needs_api_key(self._model_level) and self._api_key:
            provider = create_model(level=self._model_level, api_key=self._api_key)
        else:
            provider = create_model(level=self._model_level)
        message_history = _build_message_history(history)

        # Compute effective tools once: static tools + per-request tools from
        # the factory (which captures client_id lexically so delegation works
        # even across the claude_sdk/MCP execution-context boundary).
        effective_tools: list[Any] = list(self._tools) if self._tools else []
        if self._request_tools_factory and client_id:
            effective_tools.extend(self._request_tools_factory(client_id))
        tools_arg = effective_tools or None

        # Build the user-prompt once: plain str (no images) or a multimodal
        # list (text + BinaryContent parts). NOTE: the default model_level 3
        # routes to robotsix_llmio's claude_sdk model, whose internal
        # _content_to_text() flattens non-text content to str(...) — images
        # are silently dropped on that path. To have the assistant actually
        # *see* images, configure a vision-capable OpenRouter model at level
        # 1 or 2. Full level-3 image support requires an external change to
        # robotsix_llmio's claude_sdk model to map image parts into the
        # Claude SDK request format.
        if images:
            from pydantic_ai.messages import BinaryContent

            user_prompt: list[str | BinaryContent] = []
            if llm_message:
                user_prompt.append(llm_message)
            for mt, data in images:
                user_prompt.append(BinaryContent(data=data, media_type=mt))
            prompt: object = user_prompt
        else:
            prompt = llm_message

        on_activity = self._activity_callback(session_id)
        result: Any = None

        # Stamp model_level as trace metadata so by-model cost breakdowns
        # are usable in Langfuse — the trace_metadata dict from the caller
        # (if any) is merged in so caller keys win on collision.
        effective_trace_metadata: dict[str, str] = {
            "model_level": str(self._model_level),
        }
        if trace_metadata:
            effective_trace_metadata.update(trace_metadata)

        # Build a fresh handle per attempt so each try starts from a clean
        # state.  transient detection is delegated to is_openrouter_transient
        # so the retry loop only reacts to OpenRouter-level blips; non-transient
        # errors (including ClaudeSDKUsageExhaustedError) propagate immediately.
        async def _attempt() -> Any:
            handle = provider.build_agent(
                level=self._model_level,
                system_prompt=system_prompt,
                tools=tools_arg,
                builtin_tools=False,
                # Read-only web access. The filesystem and shell stay denied;
                # these two only fetch. Without them a research subsession
                # cannot look anything up, and — because a refused tool call
                # is indistinguishable from an empty result — it reports
                # "sources fetched, all empty" rather than "I cannot search".
                web_tools=True,
            )
            try:
                with (
                    _trace_session(
                        session_id, effective_trace_metadata, trace_name=trace_name
                    ),
                    _activity_context(on_activity),
                ):
                    return await handle.run(prompt, message_history=message_history)
            finally:
                handle.close()

        try:
            result = await acall_with_retry(
                _attempt,
                config=RetryConfig(
                    backoff_base=0.5,
                    backoff_cap=1.0,
                    max_retries=2,
                    jitter_factor=0.0,
                ),
                is_transient_fn=is_openrouter_transient,
                what="chat turn",
            )
        except (ClaudeSDKUsageExhaustedError, ClaudeSDKAuthError) as exc:
            # Two different causes, one conclusion: this tier cannot serve the
            # turn, and no retry against it will change that — credits stay
            # exhausted until they reset, a dead credential stays dead until a
            # human re-authenticates. Falling back keeps the conversation alive
            # through either.
            logger.warning(
                "model_level %d cannot serve this turn (%s: %s) — "
                "falling back to another tier",
                self._model_level,
                type(exc).__name__,
                exc,
            )
            result = await self._run_with_tier_fallback(
                prompt,
                message_history,
                tools_arg,
                session_id,
                effective_trace_metadata,
                trace_name=trace_name,
                credential_is_dead=isinstance(exc, ClaudeSDKAuthError),
            )

        # The loop above always either raises or breaks with `result` set.
        text = result.output
        # Persist the exchange in the background so memory consolidation never
        # blocks the reply. The task is tracked to avoid premature GC.
        if text:
            self._schedule_remember(message, text, session_id)
            yield text

    async def _run_with_tier_fallback(
        self,
        prompt: object,
        message_history: list[Any] | None,
        tools_arg: list[Any] | None,
        session_id: str | None,
        trace_metadata: dict[str, str] | None = None,
        *,
        trace_name: str | None = None,
        credential_is_dead: bool = False,
    ) -> Any:
        """Retry the same turn at a different tier when this one cannot serve.

        Triggered by
        :class:`~robotsix_llmio.claude_sdk.ClaudeSDKUsageExhaustedError` or
        :class:`~robotsix_llmio.claude_sdk.ClaudeSDKAuthError` at
        ``self._model_level``. Reuses robotsix-llmio's tier-escalation
        machinery
        (:func:`~robotsix_llmio.core.tier_fallback.acall_with_tier_fallback` —
        higher-then-lower, revisit-avoiding, depth-bounded) rather than
        hand-rolling a fallback chain, so it is entered ONLY once one of those
        two causes has already been identified — any other failure during the
        primary attempt still raises immediately as before.

        *credential_is_dead* distinguishes the two causes, because they need
        different reach:

        * **Usage exhaustion** is per-tier, so one promotion is enough —
          claudeSDK level 4 (fable) -> level 3 (opus) leaves the exhausted
          tier behind.
        * **An expired credential is shared by every claudeSDK tier**, since
          they all drive the same ``claude`` CLI against the same
          ``.credentials.json``. A single promotion would land on level 3 and
          fail identically. Recovery means walking down to a keyed provider,
          so the depth is widened to reach one. The intervening keyless tier
          is still attempted and still fails — but it now fails *fast*
          (``ClaudeSDKAuthError`` is not transient, so it burns no retries)
          rather than being skipped by logic that would have to hard-code
          which providers share a credential.

        Note: ``acall_with_tier_fallback`` always retries its *starting* level
        once before escalating (it has no way to know this level was already
        just attempted). That first retry is expected to fail identically and
        fail fast — a harmless, cheap redundant call, not a bug — before the
        loop falls back to the next tier.
        """
        tier_config = TierConfig()
        level_by_model = {
            getattr(tier_config, level.value).model: int(
                level.value.removeprefix("level")
            )
            for level in TierLevel
        }

        on_activity = self._activity_callback(session_id)

        def _fn_factory(tlc: TierLevelConfig) -> Callable[[], Any]:
            level = level_by_model[tlc.model]

            async def _call() -> Any:
                # The fallback tier may need a key even when the primary did
                # not — that is the whole point when the shared claudeSDK
                # credential is what died. Keyless providers reject an
                # api_key, so ask per level rather than reusing the primary's
                # answer.
                if level_needs_api_key(level) and self._api_key:
                    fallback_provider = create_model(level=level, api_key=self._api_key)
                else:
                    fallback_provider = create_model(level=level)
                fallback_handle = fallback_provider.build_agent(
                    level=level,
                    system_prompt=self._instruction,
                    tools=tools_arg,
                    builtin_tools=False,
                    web_tools=True,  # see the primary handle above
                )
                try:
                    with (
                        _trace_session(
                            session_id, trace_metadata, trace_name=trace_name
                        ),
                        _activity_context(on_activity),
                    ):
                        return await fallback_handle.run(
                            prompt, message_history=message_history
                        )
                finally:
                    fallback_handle.close()

            return _call

        return await acall_with_tier_fallback(
            _fn_factory,
            tier_config=tier_config,
            level=TierLevel(f"level{self._model_level}"),
            fallback_enabled=True,
            # A dead credential can take every claudeSDK tier with it, so the
            # walk must be able to reach a keyed provider; usage exhaustion is
            # per-tier and needs only the one step it has always taken.
            max_fallback_depth=(
                len(TierLevel) - 1 if credential_is_dead else _USAGE_FALLBACK_DEPTH
            ),
            what=(
                "chat turn (auth-failure fallback)"
                if credential_is_dead
                else "chat turn (usage-exhausted fallback)"
            ),
        )

    def _schedule_remember(
        self, message: str, reply: str, session_id: str | None
    ) -> None:
        """Fire-and-forget the memory write for a completed exchange."""
        try:
            task = asyncio.create_task(
                self._memory.remember(message, reply, session_id=session_id)
            )
        except RuntimeError:
            # No running loop (shouldn't happen in the ASGI path) — skip silently.
            return
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)
