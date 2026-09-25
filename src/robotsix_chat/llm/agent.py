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

import contextlib
import json
import logging
from collections.abc import Iterator
from typing import Any

from robotsix_llmio.claude_sdk import (
    is_claude_sdk_transient,
)
from robotsix_llmio.config.tier import TierLevelConfig
from robotsix_llmio.openrouter import is_openrouter_transient

from robotsix_chat.config import slot_needs_api_key

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
    function: str | None = None,
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

    When *function* is supplied, it is stamped as a Langfuse trace tag to
    enable cost attribution and per-function anomaly detection. Each trace
    should identify its function (e.g. ``"chat"``, ``"subsession"``,
    ``"feedback-runner"``).
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
            if function:
                _stamp_function_tag(function)
            yield
    elif session_id:
        # No custom name — use session-based grouping (existing behavior).
        with langfuse_session(session_id):
            if trace_metadata:
                _stamp_trace_metadata(trace_metadata)
            if function:
                _stamp_function_tag(function)
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


def _stamp_function_tag(function: str) -> None:
    """Stamp the function name as a Langfuse trace tag for cost attribution.

    Tags the current trace with the function name so traces can be aggregated
    by function for baseline trending and anomaly detection. A no-op when
    OpenTelemetry is absent or no span is currently recording — the tag is
    best-effort observability for cost tracking, not critical to the run.
    """
    try:
        from robotsix_llmio.core.tracing import get_recording_span
    except ImportError:
        return
    span = get_recording_span()
    if span is not None:
        span.set_attribute("langfuse.trace.tags", json.dumps([function]))
