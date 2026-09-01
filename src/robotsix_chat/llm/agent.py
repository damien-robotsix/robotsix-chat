"""LLM chat agent backed by robotsix-llmio's per-level model factory.

:class:`LlmioChatAgent` satisfies the chat server's ``ChatAgent`` protocol
(``async def stream(message) -> AsyncIterator[str]``). It selects the backend
purely from a capability **level** via
:func:`robotsix_llmio.config.create_model`: the level encodes the combined
``provider-model`` identifier (resolved from llmio's tier config), so this
package never names a concrete provider class or model.

Levels (1 cheap/frequent, 2 workhorse, 3 frontier) are a pure capability
axis. Provider redundancy is llmio's failover axis: every turn runs through
:func:`robotsix_llmio.core.failover.acall_with_failover`, which serves the
level on the keyless default slot (Claude SDK via the logged-in ``claude``
CLI) and retries the SAME level on the keyed OpenRouter fallback slot when
the default fails in a provider-shaped way — arming a sticky failover window
after repeated failures.

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
from robotsix_llmio.config import load_tier_config
from robotsix_llmio.config.tier import TierLevelConfig
from robotsix_llmio.core.factory import get_provider_for_identifier
from robotsix_llmio.core.failover import acall_with_failover
from robotsix_llmio.exceptions import ProviderExhaustedError
from robotsix_llmio.openrouter import is_openrouter_transient

from robotsix_chat.chat.actions import (
    actions_from_messages,
    current_actions,
    format_action,
    record_action,
)
from robotsix_chat.chat.events import EventSink, activity_frame
from robotsix_chat.config import slot_needs_api_key
from robotsix_chat.memory import ChatMemory, NullMemory

logger = logging.getLogger(__name__)

# Prepended to the system prompt on a keyed (OpenRouter) slot attempt when
# the turn carries prior context. A keyed provider does not share the Claude
# SDK's resume session — it sees ONLY the explicit ``message_history`` — so a
# terse follow-up ("ok, file a ticket and watch") can read to it as a fresh,
# topicless request and it replies "I don't have full context on what 'it'
# refers to" (observed 2026-08-30 on a usage-exhausted fallback). The note
# tells it plainly it is mid-conversation so it uses the provided turns
# instead of asking the user to restate the topic.
_FALLBACK_CONTINUATION_NOTE = (
    "You are continuing an ongoing conversation; the prior turns are provided "
    "as message history. Do not ask the user to restate the topic or clarify "
    "what a pronoun refers to — read the prior turns for that context."
)

# Per-run model-request cap for KEYED (OpenRouter) slot attempts.
# pydantic-ai's default UsageLimits(request_limit=50) counts every tool-call
# round-trip as a request; a tool-heavy chat turn legitimately needs far more
# than 50 of them. The Claude SDK slot never hits the cap (the CLI runs the
# agent loop internally, so pydantic-ai sees ~one request per turn) — which
# is why this only surfaced during subscription exhaustion, when turns
# degrade to the keyed slot: complex turns died mid-stream with
# "UsageLimitExceeded: The next request would exceed the request_limit of
# 50", shown to the user as a raw internal error (observed 2026-09-01 under
# the weekly Claude cap). 200 keeps a runaway loop bounded while clearing
# every legitimate turn seen in the incident logs. The SDK tool path
# warns-and-drops run kwargs it cannot honor, so the limits are passed only
# to keyed slots.
_KEYED_REQUEST_LIMIT = 200


def _keyed_usage_limits(tlc: TierLevelConfig) -> Any:
    """``usage_limits`` for ``handle.run`` — set only on keyed slots."""
    if not slot_needs_api_key(tlc):
        return None
    from pydantic_ai.usage import UsageLimits

    return UsageLimits(request_limit=_KEYED_REQUEST_LIMIT)


def _merge_tier_overrides(
    tier_overrides: dict[str, Any] | None,
    failover_window_seconds: float | None,
) -> dict[str, Any]:
    """Build one ``load_tier_config`` dict from the operator's overrides.

    Combines the ``llmio_tier_overrides`` setting with the failover-window
    knob.

    The window is layered onto (a copy of) any ``failover`` section the
    operator supplied; provider/level binding decisions live entirely in the
    setting (e.g. the 2026-09-01 operator override binding chat's fallback
    level 2 to the pro snapshot), never in code.
    """
    overrides: dict[str, Any] = dict(tier_overrides or {})
    if failover_window_seconds is not None:
        failover = dict(overrides.get("failover") or {})
        failover["window_seconds"] = failover_window_seconds
        overrides["failover"] = failover
    return overrides


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
    propagating to llmio's provider-failover loop instead of burning
    retries.
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

    INVARIANT: replayed history is text-only — turns are ``(str, str)`` and
    every part built here is ``UserPromptPart(str)`` / ``TextPart(str)``.
    Never let a binary part (an image from an earlier turn) into this list:
    a single ``BinaryContent`` in history 404s the whole turn on text-only
    OpenRouter models ("No endpoints found that support image input"), so an
    old attachment would poison every later turn of the session. Attachments
    are per-turn only, via ``build_agent(images=...)``.
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


def _chained_claude_unavailability(
    exc: BaseException,
) -> ClaudeSDKUsageExhaustedError | ClaudeSDKAuthError | None:
    """Return the exhaustion/auth root buried in *exc*'s cause/context chain.

    The Claude CLI sometimes launders a usage-limit failure into a generic
    ``Exception("Claude Code returned an error result: success")`` that only
    carries the typed error as ``__context__`` — a bare ``except`` on the
    typed pair misses it and the turn dies without ever reaching the
    fallback slot (operator-reported). Walks both ``__cause__`` and
    ``__context__``, bounded against cycles.
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
        failover_window_seconds: float | None = None,
        tier_overrides: dict[str, Any] | None = None,
    ) -> None:
        """Store the agent configuration for later ``stream`` calls.

        *api_key* is the configured OpenRouter key, if any. It is forwarded
        only to keyed (OpenRouter) slot attempts — holding it is what lets
        llmio's provider failover reach the keyed fallback slot when the
        shared Claude credential or subscription quota is the thing that
        failed.

        *task_budget_tokens*, when set, is forwarded as ``max_tokens`` to
        keyless (claudeSDK) slot attempts only. llmio maps that onto the
        Claude Agent SDK ``task_budget`` advisory allowance — the countdown
        the model reads so it can pace itself against a real
        budget-remaining signal instead of being cut off at the subscription
        limit. Keyed (OpenRouter) slots are deliberately left alone: their
        own per-response ``max_tokens`` caps live in llmio's tier config and
        must not be clobbered.

        *failover_window_seconds*, when set, overrides how long llmio routes
        calls straight to the fallback (OpenRouter) slot after the default
        (Claude) slot fails repeatedly, before automatically returning to
        the default. ``None`` keeps llmio's baked policy (15 minutes).

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
        # The two-slot tier config every turn resolves against; its failover
        # section is adopted by llmio's process-wide tracker on each call.
        # Binding overrides come from the llmio_tier_overrides setting; the
        # raw dict stays readable (create_app mirrors it onto app.state for
        # the /models display, following the model_level pattern).
        self.tier_overrides: dict[str, Any] = dict(tier_overrides or {})
        self._tier_config = load_tier_config(
            _merge_tier_overrides(self.tier_overrides, failover_window_seconds)
        )
        # Hold references to in-flight background writes so they aren't GC'd.
        self._write_tasks: set[asyncio.Task[None]] = set()

    @property
    def memory(self) -> ChatMemory:
        """The agent's memory backend (for health reporting / recovery wiring)."""
        return self._memory

    def _slot_kwargs(self, tlc: TierLevelConfig) -> dict[str, Any]:
        """Build the provider-constructor kwargs for the slot binding *tlc*.

        Starts from the slot's own ``provider_kwargs`` + ``max_tokens`` (a
        real per-response cap on OpenRouter). Forwards the configured
        api_key only to keyed (OpenRouter) slots — keyless providers reject
        it. On keyless (claudeSDK) slots, ``task_budget_tokens`` becomes the
        advisory ``task_budget`` via ``max_tokens``.
        """
        kwargs: dict[str, Any] = dict(tlc.provider_kwargs)
        if tlc.max_tokens is not None:
            kwargs.setdefault("max_tokens", tlc.max_tokens)
        if slot_needs_api_key(tlc):
            if self._api_key:
                kwargs["api_key"] = self._api_key
        elif self._task_budget_tokens is not None:
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
        ``[("image/png", b"...")]``) — attachments are handed to llmio's
        ``build_agent(images=...)`` seam: the Claude transport reads them
        natively, while text-only OpenRouter models get an ``ask_image``
        tool (answered by the tier config's vision binding) instead. Images
        belong to the turn they arrive with only — replayed history is
        text-only by construction, so an old attachment can never poison a
        later turn.
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

        Transient upstream errors (OpenRouter provider failures, 5xx,
        network blips) are retried locally per slot attempt. Provider-shaped
        failures that outlive those retries (usage exhaustion, a dead
        credential, an outage) are handed to llmio's provider failover,
        which retries the SAME level on the other provider slot and arms a
        sticky failover window on repeated default-slot failures. Only when
        both slots fail does the error surface — the chat server turns that
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

        # The per-call override wins; the slot factory and the trace
        # metadata below close over this local, so nothing else has to be
        # threaded. Which provider serves the level is llmio's business:
        # every turn goes through acall_with_failover, which resolves the
        # active slot (and retries the other slot on provider failure).
        level = self._model_level if model_level is None else model_level
        message_history = _build_message_history(history)

        # Compute effective tools once: static tools + per-request tools from
        # the factory (which captures client_id lexically so delegation works
        # even across the claude_sdk/MCP execution-context boundary).
        effective_tools: list[Any] = list(self._tools) if self._tools else []
        if self._request_tools_factory and client_id:
            effective_tools.extend(self._request_tools_factory(client_id))
        tools_arg = effective_tools or None

        # The prompt is ALWAYS plain text. Attachments travel through
        # llmio's ``build_agent(images=...)`` seam instead of being embedded
        # as ``BinaryContent`` parts: the claude_sdk transport passes them as
        # native SDK image blocks (Claude models read images directly), and
        # text-only OpenRouter models get an injected ``ask_image`` tool
        # answered by the tier config's vision binding. Embedding binary
        # parts here 404'd every DeepSeek-served turn ("No endpoints found
        # that support image input", live incident 2026-09-01).
        prompt = llm_message

        on_activity = self._activity_callback(session_id)

        # Stamp model_level as trace metadata so by-model cost breakdowns
        # are usable in Langfuse — the trace_metadata dict from the caller
        # (if any) is merged in so caller keys win on collision.
        effective_trace_metadata: dict[str, str] = {
            "model_level": str(level),
        }
        if trace_metadata:
            effective_trace_metadata.update(trace_metadata)

        # One factory per provider slot: llmio's failover loop calls it with
        # the slot's TierLevelConfig for THIS level (the level never changes
        # across attempts — only the provider slot does). Each slot attempt
        # runs under its own bounded transient retry; a provider-shaped
        # failure that outlives the retries bubbles to the failover loop,
        # which records it and retries the same level on the other slot.
        def _fn_factory(tlc: TierLevelConfig) -> Callable[[], Any]:
            async def _attempt() -> Any:
                provider = get_provider_for_identifier(
                    tlc.model, **self._slot_kwargs(tlc)
                )
                slot_system_prompt = system_prompt
                effective_history = message_history
                if slot_needs_api_key(tlc):
                    # A keyed (OpenRouter) slot does not share the Claude
                    # SDK resume session — tell it plainly it is
                    # mid-conversation, and cap the replayed history to the
                    # slot's token window.
                    if message_history:
                        slot_system_prompt = (
                            f"{system_prompt}\n\n{_FALLBACK_CONTINUATION_NOTE}"
                        )
                    if history and tlc.max_tokens:
                        capped = _cap_history_for_keyed_tier(history, tlc.max_tokens)
                        effective_history = _build_message_history(capped)
                handle = provider.build_agent(
                    level=level,
                    model=tlc.model_name,
                    # Only the images/vision path reads tier_config here —
                    # the explicit ``model=`` override bypasses level
                    # resolution — so passing it wires the configured vision
                    # binding without touching slot routing.
                    tier_config=self._tier_config,
                    system_prompt=slot_system_prompt,
                    tools=tools_arg,
                    images=images or None,
                    vision_api_key=self._api_key or None,
                    builtin_tools=False,
                    # Read-only web access. The filesystem and shell stay
                    # denied; these two only fetch. Without them a research
                    # subsession cannot look anything up, and — because a
                    # refused tool call is indistinguishable from an empty
                    # result — it reports "sources fetched, all empty"
                    # rather than "I cannot search".
                    web_tools=True,
                )
                # Stateless turn: history + one fresh memory block travel in
                # the prompt; no CLI session is resumed (see the module note
                # above).
                try:
                    with (
                        _trace_session(
                            session_id,
                            effective_trace_metadata,
                            trace_name=trace_name,
                        ),
                        _activity_context(on_activity),
                    ):
                        limits = _keyed_usage_limits(tlc)
                        if limits is not None:
                            return await handle.run(
                                prompt,
                                message_history=effective_history,
                                usage_limits=limits,
                            )
                        return await handle.run(
                            prompt, message_history=effective_history
                        )
                finally:
                    handle.close()

            async def _run_slot() -> Any:
                try:
                    return await acall_with_retry(
                        _attempt,
                        config=RetryConfig(
                            backoff_base=0.5,
                            backoff_cap=1.0,
                            max_retries=2,
                            jitter_factor=0.0,
                        ),
                        is_transient_fn=_is_chat_turn_transient,
                        what=f"chat turn ({tlc.provider})",
                    )
                except Exception as exc:
                    # A dead Claude credential takes the whole default slot
                    # with it (every claudeSDK level drives the same CLI and
                    # .credentials.json), but llmio's failover classifier
                    # has no auth category — wrap it as a provider-wide
                    # exhaustion so the loop arms failover immediately and
                    # retries this level on the keyed slot. Usage exhaustion
                    # needs no wrapping: llmio detects it in the
                    # cause/context chain even when the CLI launders it.
                    root = _chained_claude_unavailability(exc)
                    if isinstance(root, ClaudeSDKAuthError):
                        raise ProviderExhaustedError(
                            f"claude credential unusable: {root}"
                        ) from exc
                    raise

            return _run_slot

        result = await acall_with_failover(
            _fn_factory,
            tier_config=self._tier_config,
            level=level,
            what="chat turn",
        )

        self._record_actions_from_result(result)
        text = result.output
        # Persist the exchange in the background so memory consolidation never
        # blocks the reply. The task is tracked to avoid premature GC.
        if text:
            self._schedule_remember(message, text, session_id)
            yield text

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
