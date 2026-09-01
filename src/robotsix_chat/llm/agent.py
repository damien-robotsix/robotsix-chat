"""LLM chat agent backed by robotsix-llmio's per-level model factory.

:class:`LlmioChatAgent` satisfies the chat server's ``ChatAgent`` protocol
(``async def stream(message) -> AsyncIterator[str]``). It selects the backend
purely from a capability **level** via
:func:`robotsix_llmio.config.create_model`: the level encodes the combined
``provider-model`` identifier (resolved from llmio's baked default
``TierLevelConfig``), so this package never names a concrete provider class or
model.

Levels 1 and 3 use a keyed provider (needs an API key); levels 2, 4 and 5 use
the keyless Claude SDK (via the logged-in ``claude`` CLI).

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
    is_claude_sdk_transient,
)
from robotsix_llmio.config import create_model
from robotsix_llmio.config.tier import TierConfig, TierLevel, TierLevelConfig
from robotsix_llmio.core.tier_fallback import acall_with_tier_fallback
from robotsix_llmio.openrouter import is_openrouter_transient

from robotsix_chat.chat.actions import (
    actions_from_messages,
    current_actions,
    format_action,
    record_action,
)
from robotsix_chat.chat.events import EventSink, activity_frame
from robotsix_chat.config import level_needs_api_key
from robotsix_chat.memory import ChatMemory, NullMemory

logger = logging.getLogger(__name__)

# Promotions allowed when a tier reports exhausted usage credits. This used
# to be 1, on the assumption that "the very next tier is a working one". That
# held for the four-tier map (level 4 fable -> level 3 opus, different
# limits). Under the five-tier map every Claude level (2 haiku, 4 opus,
# 5 fable) draws on ONE subscription cap: when level 5 is exhausted, level 4
# is too, and a depth-1 walk from 5 dies on 4 with "fallback depth (1)
# exhausted" — surfaced to the user as "The assistant hit an internal error"
# (observed 2026-08-29 22:04Z and again 23:28Z). The walk must be able to
# reach a keyed provider (level 3 mimo); the intervening Claude tier is
# already in llmio's cooldown and is skipped without a call. Operator's
# rule for chat: Claude first, graceful paid fallback when Claude is depleted.
# Two promotions reach the keyed level 3 (mimo) from any Claude tier — the
# higher-then-lower walk visits the sibling Claude tier (in llmio's cooldown,
# fails fast) and then mimo — and stop there. Walking further lands on the
# tier-1/2 models, which cannot carry a long agentic chat context: a flash
# turn on a 20k-token session disowns the whole conversation ("digest
# corrupted"), which is worse than failing the turn with a clear error.
_USAGE_FALLBACK_DEPTH = 2

# Prepended to a keyed (non-SDK) fallback tier's system prompt when the turn
# carries prior context. A keyed provider does not share the Claude SDK's
# resume session — it sees ONLY the explicit ``message_history`` — so a terse
# follow-up ("ok, file a ticket and watch") can read to it as a fresh, topicless
# request and it replies "I don't have full context on what 'it' refers to"
# (observed 2026-08-30 on a usage-exhausted fallback to level 3). The note tells
# it plainly it is mid-conversation so it uses the provided turns instead of
# asking the user to restate the topic.
_FALLBACK_CONTINUATION_NOTE = (
    "You are continuing an ongoing conversation; the prior turns are provided "
    "as message history. Do not ask the user to restate the topic or clarify "
    "what a pronoun refers to — read the prior turns for that context."
)

# Per-run model-request cap for KEYED (OpenRouter) tiers. pydantic-ai's
# default UsageLimits(request_limit=50) counts every tool-call round-trip as
# a request; a tool-heavy chat turn legitimately needs far more than 50 of
# them. The Claude SDK tiers never hit the cap (the CLI runs the agent loop
# internally, so pydantic-ai sees ~one request per turn) — which is why this
# only surfaced during subscription exhaustion, when turns degrade to the
# keyed tier: complex turns died mid-stream with "UsageLimitExceeded: The
# next request would exceed the request_limit of 50", shown to the user as
# a raw internal error (observed 2026-09-01 under the weekly Claude cap).
# 200 keeps a runaway loop bounded while clearing every legitimate turn
# seen in the incident logs. The SDK tool path warns-and-drops run kwargs
# it cannot honor, so the limits are passed only to keyed tiers.
_KEYED_REQUEST_LIMIT = 200


def _keyed_usage_limits(level: int) -> Any:
    """``usage_limits`` for ``handle.run`` — set only on keyed tiers."""
    if not level_needs_api_key(level):
        return None
    from pydantic_ai.usage import UsageLimits

    return UsageLimits(request_limit=_KEYED_REQUEST_LIMIT)


# A prior conversation turn replayed to the agent: ``(user, assistant)``.
Turn = tuple[str, str]


def _is_chat_turn_transient(exc: BaseException) -> bool:
    """Return True when *exc* warrants retrying the chat turn.

    Covers both OpenRouter-level blips (timeouts, upstream provider errors,
    429/5xx) and Claude Agent SDK transport/control failures — the
    degenerate-success frame, a lost control-protocol connection, and the
    per-call wall-clock timeout on a stalled run.  Usage-exhaustion and auth
    failures are deliberately excluded (checked first inside
    :func:`~robotsix_llmio.claude_sdk.is_claude_sdk_transient`) so they keep
    propagating to the tier-fallback path instead of burning retries.
    """
    return is_openrouter_transient(exc) or is_claude_sdk_transient(exc)


# NOTE on Claude SDK sessions: chat turns are deliberately STATELESS per call.
# An earlier design resumed one CLI session per chat session (``resume=``) to
# reuse the CLI's prompt cache — but a resumed transcript keeps every previous
# prompt verbatim, and each prompt already carried the rendered history plus
# that turn's recalled-memory block. The model therefore saw N copies of the
# history and N stale memory blocks by turn N (measured 2026-08-29: one chat
# session's transcript held 69 prompts, 68 memory blocks, 1051 embedded
# ``User:`` labels, prompt 7.5k → 157k chars). Sending system prompt + raw
# history + ONE fresh memory block + the new message every turn keeps the
# context bounded and the memory block current; the static prefix (system
# prompt, tools) still caches at the provider.


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


# Fraction of a keyed tier's token window the history may consume.  The
# remaining 30 % is reserved for the system prompt, tools, the current user
# turn, and the model's response tokens.  Derived from the level-3 (mimo)
# window of 65 536 tokens — a 20 k-token history + system prompt already
# pushed past the limit (observed 2026-08-31, correlation d6ad6be).
_HISTORY_TOKEN_BUDGET_FRACTION = 0.70

# Note prepended to the first surviving user turn when older turns were
# dropped to fit the fallback tier's context window.
_HISTORY_OMISSION_NOTE = (
    "[Older conversation turns were omitted to fit the model's context window.]"
)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate using a chars/4 heuristic.

    Good enough for deciding how many turns to keep — the real tokenizer is
    provider-specific and not worth pulling in as a dependency for a
    best-effort cap.
    """
    return max(1, len(text) // 4)


def _cap_history_for_keyed_tier(
    history: list[Turn],
    max_tokens: int,
) -> list[Turn]:
    """Drop the oldest turns from *history* until it fits *max_tokens*.

    The budget is ``_HISTORY_TOKEN_BUDGET_FRACTION`` of *max_tokens* so the
    system prompt, tools and current turn have room.  The most recent turn is
    always kept (even if it alone exceeds the budget).  When turns are dropped,
    the first surviving user message is prefixed with
    :data:`_HISTORY_OMISSION_NOTE` so the fallback model knows context was
    trimmed.
    """
    if not history or max_tokens <= 0:
        return history

    budget = int(max_tokens * _HISTORY_TOKEN_BUDGET_FRACTION)

    # Walk from the newest turn backwards until we fit.
    kept: list[Turn] = []
    running = 0
    for user_msg, asst_msg in reversed(history):
        turn_tokens = _estimate_tokens(user_msg) + _estimate_tokens(asst_msg)
        if kept and running + turn_tokens > budget:
            break
        kept.append((user_msg, asst_msg))
        running += turn_tokens
    kept.reverse()

    if len(kept) < len(history):
        first_user, first_asst = kept[0]
        kept[0] = (f"{_HISTORY_OMISSION_NOTE}\n{first_user}", first_asst)
        logger.info(
            "Capped fallback history: kept %d/%d turns (%d est. tokens, "
            "budget %d) to fit tier window of %d tokens",
            len(kept),
            len(history),
            running,
            budget,
            max_tokens,
        )

    return kept


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


def _primary_in_cooldown(level: int) -> bool:
    """Return ``True`` when *level*'s Claude model is already cooling.

    llmio's health tracker learns about exhaustion from the fallback walks;
    consulting it before the PRIMARY attempt saves a doomed ~2s CLI probe on
    every turn of every session while the subscription is depleted. Fails
    open — any error means "not in cooldown".
    """
    try:
        tlc = getattr(TierConfig(), f"level{level}")
        if not str(tlc.model).startswith("claudeSDK"):
            return False
        from robotsix_llmio.core import get_health_tracker

        return bool(get_health_tracker().is_in_cooldown(tlc.model))
    except Exception:
        return False


def _chained_claude_unavailability(
    exc: BaseException,
) -> ClaudeSDKUsageExhaustedError | ClaudeSDKAuthError | None:
    """Return the exhaustion/auth root buried in *exc*'s cause/context chain.

    The Claude CLI sometimes launders a usage-limit failure into a generic
    ``Exception("Claude Code returned an error result: success")`` that only
    carries the typed error as ``__context__`` — a bare ``except`` on the
    typed pair misses it and the turn dies without ever trying the fallback
    tiers (operator-reported). Walks both ``__cause__`` and ``__context__``,
    bounded against cycles.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 32:
        if isinstance(cur, ClaudeSDKUsageExhaustedError | ClaudeSDKAuthError):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return None


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
        task_budget_tokens: int | None = None,
    ) -> None:
        """Store the agent configuration for later ``stream`` calls.

        *api_key* is the configured OpenRouter key, if any — **not** a key for
        *model_level* specifically. Pass it even when *model_level* is a
        keyless (claudeSDK) tier: it is forwarded only to levels whose
        provider actually takes one, and holding it is what lets a tier
        fallback reach a keyed provider when the shared Claude credential is
        the thing that failed.

        *task_budget_tokens*, when set, is forwarded as ``max_tokens`` to
        keyless (claudeSDK) tiers only. llmio maps that onto the Claude Agent
        SDK ``task_budget`` advisory allowance — the countdown the model reads
        so it can pace itself against a real budget-remaining signal instead
        of being cut off at the subscription limit. Keyed (OpenRouter) tiers
        are deliberately left alone: their own per-response ``max_tokens``
        caps live in llmio's tier config and must not be clobbered.

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
        self._task_budget_tokens = task_budget_tokens
        # Hold references to in-flight background writes so they aren't GC'd.
        self._write_tasks: set[asyncio.Task[None]] = set()

    @property
    def memory(self) -> ChatMemory:
        """The agent's memory backend (for health reporting / recovery wiring)."""
        return self._memory

    def _provider_kwargs(self, level: int) -> dict[str, Any]:
        """Build the ``create_model`` kwargs for *level*.

        Forwards the configured api_key only to keyed (OpenRouter) levels —
        keyless providers reject it. Forwards ``max_tokens`` only to keyless
        (claudeSDK) levels — there it becomes the advisory ``task_budget``;
        keyed levels keep their own per-response caps from llmio's tier
        config, which must not be clobbered.
        """
        kwargs: dict[str, Any] = {}
        if level_needs_api_key(level) and self._api_key:
            kwargs["api_key"] = self._api_key
        if not level_needs_api_key(level) and self._task_budget_tokens is not None:
            kwargs["max_tokens"] = self._task_budget_tokens
        return kwargs

    def _activity_callback(
        self, session_id: str | None
    ) -> Callable[[ClaudeSDKActivityEvent], None] | None:
        """Build the ``on_event`` callback for :func:`activity_events`.

        Bound to *session_id*.  The callback does two things: publishes an
        activity frame to the event sink (when one is configured and there
        is a session to scope it to), and records ``tool_call`` /
        ``tool_result`` pairs into the caller's actions collector (see
        :func:`robotsix_chat.chat.actions.collect_actions`) when one is
        active.  Returns ``None`` when neither applies so the caller can
        skip wrapping the run in a no-op context.
        """
        sink = self._event_sink if session_id else None
        actions = current_actions()
        if sink is None and actions is None:
            return None

        # A tool_call event carries the name + args; the matching tool_result
        # arrives as a separate event without the name.  Record the call
        # immediately and patch its entry when the result shows up so a call
        # that never returns (turn aborted) still leaves a trace.
        pending: list[tuple[int, str, str]] = []

        def _record(event: ClaudeSDKActivityEvent) -> None:
            if actions is None:
                return
            if event.kind == "tool_call" and event.tool_name:
                before = len(actions)
                record_action(event.tool_name, event.detail, entries=actions)
                if len(actions) > before:
                    pending.append((before, event.tool_name, event.detail))
            elif event.kind == "tool_result" and pending:
                idx, name, args = pending.pop(0)
                actions[idx] = format_action(
                    name, args, event.detail, is_error=event.is_error
                )

        def _on_activity(event: ClaudeSDKActivityEvent) -> None:
            _record(event)
            if sink is not None and session_id:
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

    @staticmethod
    def _record_actions_from_result(result: Any) -> None:
        """Fill the active actions collector from a run result's messages.

        The keyed (pydantic-ai) tiers report tool calls only through
        ``result.all_messages()`` — no activity events fire for them.  When
        the collector is still empty after the run, extract the
        ``ToolCallPart`` / ``ToolReturnPart`` pairs from there.  A collector
        already populated by activity events is left as is (the SDK tiers
        would otherwise double-count).  Never raises.
        """
        actions = current_actions()
        if actions is None or actions or result is None:
            return
        all_messages = getattr(result, "all_messages", None)
        if not callable(all_messages):
            return
        try:
            messages = all_messages()
        except Exception:
            return
        actions.extend(actions_from_messages(messages))

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

    @property
    def model_level(self) -> int:
        """The level this agent was configured with.

        Read-only: a session's escalated level is passed per turn via
        ``stream(model_level=...)`` rather than mutating the shared agent.
        """
        return self._model_level

    @property
    def has_api_key(self) -> bool:
        """Whether a non-empty API key is configured for keyed levels.

        Keyless (claudeSDK) levels never need it; keyed (OpenRouter) levels
        cannot serve a turn without it. Surfaced so the UI can mark keyed
        model levels unavailable when no key is present.
        """
        return bool(self._api_key)

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
        model_level: int | None = None,
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

        *model_level* overrides the agent's configured level for this turn
        only.  The chat server passes a session's escalated level here, so one
        shared agent instance can serve sessions running at different tiers
        without rebuilding it.

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
        # The per-call override wins; ``_attempt`` and the trace metadata below
        # close over this local, so nothing else has to be threaded.
        level = self._model_level if model_level is None else model_level
        provider = create_model(level=level, **self._provider_kwargs(level))
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
            "model_level": str(level),
        }
        if trace_metadata:
            effective_trace_metadata.update(trace_metadata)

        # Build a fresh handle per attempt so each try starts from a clean
        # state.  Transient detection is delegated to _is_chat_turn_transient:
        # OpenRouter-level blips AND Claude Agent SDK transport/control
        # failures (degenerate-success frame, dropped control connection,
        # stalled-run timeout) are retried; non-transient errors (including
        # ClaudeSDKUsageExhaustedError and ClaudeSDKAuthError) propagate
        # immediately to the tier-fallback path below.
        async def _attempt() -> Any:
            handle = provider.build_agent(
                level=level,
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
            # Stateless turn: history + one fresh memory block travel in the
            # prompt; no CLI session is resumed (see the module note above).
            try:
                with (
                    _trace_session(
                        session_id, effective_trace_metadata, trace_name=trace_name
                    ),
                    _activity_context(on_activity),
                ):
                    limits = _keyed_usage_limits(level)
                    if limits is not None:
                        return await handle.run(
                            prompt,
                            message_history=message_history,
                            usage_limits=limits,
                        )
                    return await handle.run(prompt, message_history=message_history)
            finally:
                handle.close()

        if _primary_in_cooldown(level):
            logger.info(
                "model_level %d is in cooldown — going straight to the fallback walk",
                level,
            )
            result = await self._run_with_tier_fallback(
                prompt,
                message_history,
                tools_arg,
                session_id,
                effective_trace_metadata,
                level=level,
                trace_name=trace_name,
                credential_is_dead=False,
                raw_history=history,
            )
        else:
            try:
                result = await acall_with_retry(
                    _attempt,
                    config=RetryConfig(
                        backoff_base=0.5,
                        backoff_cap=1.0,
                        max_retries=2,
                        jitter_factor=0.0,
                    ),
                    is_transient_fn=_is_chat_turn_transient,
                    what="chat turn",
                )
            except Exception as exc:
                # Two different causes, one conclusion: this tier cannot serve the
                # turn, and no retry against it will change that — credits stay
                # exhausted until they reset, a dead credential stays dead until a
                # human re-authenticates. Falling back keeps the conversation alive
                # through either. The root may be buried in the cause/context
                # chain (laundered CLI failures), so match the chain, not just
                # the raised type.
                root = _chained_claude_unavailability(exc)
                if root is None:
                    raise
                logger.warning(
                    "model_level %d cannot serve this turn (%s via %s: %s) — "
                    "falling back to another tier",
                    level,
                    type(root).__name__,
                    type(exc).__name__,
                    exc,
                )
                try:
                    result = await self._run_with_tier_fallback(
                        prompt,
                        message_history,
                        tools_arg,
                        session_id,
                        effective_trace_metadata,
                        level=level,
                        trace_name=trace_name,
                        credential_is_dead=isinstance(root, ClaudeSDKAuthError),
                        raw_history=history,
                    )
                except Exception as fallback_exc:
                    # Keep the root cause on the chain via an explicit __cause__:
                    # llmio's is_claude_sdk_usage_exhausted() only follows
                    # __cause__ links, and the SSE error path uses it to show the
                    # actionable quota message (reset time) instead of the generic
                    # "internal error" when the fallback walk itself also failed
                    # (e.g. OpenRouter 404 on the backup model).
                    raise fallback_exc from exc

        # The loop above always either raises or breaks with `result` set.
        self._record_actions_from_result(result)
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
        level: int | None = None,
        trace_name: str | None = None,
        credential_is_dead: bool = False,
        raw_history: list[Turn] | None = None,
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

        * **Usage exhaustion** is per-*subscription* under the five-tier map
          (haiku/opus/fable all draw on one cap), so the walk must be able to
          pass the sibling Claude tiers — already in llmio's cooldown, so
          skipped without a call — and land on the keyed level 3 (mimo). Chat
          degrades to paid tokens rather than failing the turn.
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
                fallback_provider = create_model(
                    level=level, **self._provider_kwargs(level)
                )
                # A keyed (non-SDK) fallback tier does not share the Claude SDK
                # resume session; it relies entirely on the explicit
                # ``message_history`` passed to ``run`` below. When this turn
                # carries prior context, tell the fallback plainly that it is
                # mid-conversation so it does not wipe the topic and ask the
                # user to restate it. The SDK tiers keep the bare instruction —
                # their session already frames the exchange.
                system_prompt = self._instruction
                if message_history and level_needs_api_key(level):
                    system_prompt = (
                        f"{self._instruction}\n\n{_FALLBACK_CONTINUATION_NOTE}"
                    )
                fallback_handle = fallback_provider.build_agent(
                    level=level,
                    system_prompt=system_prompt,
                    tools=tools_arg,
                    builtin_tools=False,
                    web_tools=True,  # see the primary handle above
                )
                # Cap the history for keyed (non-SDK) tiers so the prompt
                # fits within the tier's token window.  Claude SDK tiers
                # manage their own session context and must not be truncated.
                effective_history = message_history
                if raw_history and level_needs_api_key(level):
                    tlc_cfg = getattr(tier_config, f"level{level}", None)
                    tier_max = getattr(tlc_cfg, "max_tokens", None) if tlc_cfg else None
                    if tier_max:
                        capped = _cap_history_for_keyed_tier(raw_history, tier_max)
                        effective_history = _build_message_history(capped)
                try:
                    with (
                        _trace_session(
                            session_id, trace_metadata, trace_name=trace_name
                        ),
                        _activity_context(on_activity),
                    ):
                        limits = _keyed_usage_limits(level)
                        if limits is not None:
                            return await fallback_handle.run(
                                prompt,
                                message_history=effective_history,
                                usage_limits=limits,
                            )
                        return await fallback_handle.run(
                            prompt, message_history=effective_history
                        )
                finally:
                    fallback_handle.close()

            return _call

        return await acall_with_tier_fallback(
            _fn_factory,
            tier_config=tier_config,
            level=TierLevel(f"level{self._model_level if level is None else level}"),
            fallback_enabled=True,
            # Both a dead credential and an exhausted subscription take every
            # claudeSDK tier with them; two promotions reach the keyed level 3
            # from any Claude tier, and the walk deliberately stops there.
            max_fallback_depth=_USAGE_FALLBACK_DEPTH,
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
