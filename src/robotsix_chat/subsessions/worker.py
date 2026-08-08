"""Subsession worker — spawn validation, the turn loop, and startup resume.

A subsession runs as a single coroutine (`_subsession_worker`) that loops
over **agent turns**.  llmio has no mid-run message injection, so steering
messages (from the parent agent or, for ``user_chat``, from the user) are
queued in the registry inbox and drained at turn boundaries:

* ``task``    — one turn; extra turns only while steering messages arrive.
* ``user_chat`` — turn per inbox batch; waits (cancellable) between turns.
* ``periodic`` — turn per tick; sleeps on the inbox event so a steering
  message wakes it early.  ``NO_CHANGE`` replies are suppressed (not
  delivered to the parent) and auto-stop the loop after N in a row.

Every kind can end itself by calling its ``complete_subsession`` tool —
the tool flips the shared :class:`CloseState`, checked after each turn.
External closes cancel the task (plain asyncio cancellation; the agent's
``finally: handle.close()`` reaps the LLM handle).
"""

from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from robotsix_llmio.openrouter import is_openrouter_transient

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, subsession_result_frame

from .delivery import ParentDelivery
from .models import (
    InboxMessage,
    SubsessionCapacityError,
    SubsessionDedupError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionPeriodicSpawnError,
    SubsessionStatus,
    SubsessionUserChatSpawnError,
)
from .registry import SubsessionRegistry

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventSink
    from robotsix_chat.chat.server.routes import ChatAgent
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

# Prior turns replayed to the subsession agent are capped so a
# long-running periodic/user_chat subsession cannot grow its own prompt
# without bound.
_MAX_WORKER_HISTORY_TURNS = 20

# The Claude Agent SDK's wording when it collapses a self-contradictory
# ``is_error=True`` / ``errors=[]`` / ``subtype="success"`` frame into a
# bare message — a known transient bug, not a real tool failure.
_DEGENERATE_SUCCESS_SIGNATURE = "returned an error result: success"

# The Claude CLI's wording when a tier's usage credits are exhausted.
_USAGE_EXHAUSTED_SIGNATURE = "out of usage credits"


def _format_worker_error(exc: BaseException) -> str:
    """Translate known Claude SDK error patterns into clear human-readable messages.

    When *exc* is a :class:`claude_agent_sdk.ProcessError` (the CLI
    subprocess exited non-zero), the message includes the exit code and
    stderr output so the operator can diagnose the tool failure without
    digging through logs.

    For unrecognised exceptions the exception type name is always included
    so the message is actionable even when the SDK wording is opaque.
    """
    msg = str(exc)
    exc_type_name = type(exc).__name__

    # Degenerate success frame — a known transient Claude SDK bug that
    # can persist across retries.  Not a real tool failure.
    if _DEGENERATE_SUCCESS_SIGNATURE in msg.lower():
        return (
            "The Claude agent encountered a transient internal SDK error "
            "(degenerate success frame — the SDK reported an error result "
            "whose subtype is 'success', a self-contradictory frame that "
            "could not be cleared by retry). This is a known Claude SDK "
            "bug and does not indicate a real tool failure. "
            f"Original SDK message: {msg}"
        )

    # Usage-exhaustion — the tier has no credits left.
    if _USAGE_EXHAUSTED_SIGNATURE in msg.lower():
        return (
            "The Claude agent's usage credits for this tier are exhausted. "
            "Switch to a different model level, or wait for credits to "
            "reset. " + msg
        )

    # ProcessError from claude_agent_sdk carries exit_code and stderr —
    # surface those so the operator can diagnose without log-diving.
    exit_code = getattr(exc, "exit_code", None)
    if exit_code is not None:
        stderr = getattr(exc, "stderr", None)
        parts = [f"Claude CLI process exited with code {exit_code}"]
        if stderr:
            stderr_text = str(stderr).strip()
            if stderr_text:
                parts.append(f"stderr: {_truncate(stderr_text, 500)}")
        parts.append(msg)
        return "\n".join(parts)

    # For any other exception, include the type name so the message is
    # never just an opaque SDK string — the operator can distinguish a
    # TimeoutError from a RuntimeError at a glance.
    if exc_type_name not in msg:
        return f"[{exc_type_name}] {msg}"
    return msg


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* chars, appending ``"..."`` when cut."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# Reply sentinel a periodic subsession uses to report "nothing changed".
_NO_CHANGE_SENTINEL = "NO_CHANGE"


# Prompt fragment prepended when a user_chat / task subsession is retried
# after a failure.  The agent sees the original error so it can diagnose
# and self-correct (e.g. re-build context that was lost).
_RETRY_PROMPT_TEMPLATE = (
    "[System note: this subsession is being retried after a failure "
    "(attempt {attempt}/{max_retries}). The error was:\n\n{error}\n\n"
    "The subsession has been re-launched from its original instructions. "
    "If the error was caused by lost context (e.g. after a server restart) "
    "you may need to re-fetch any external state you were relying on. "
    "Your original instructions follow below.]\n\n"
)

# System note prepended to the first turn of every user_chat subsession so
# the agent always restates option definitions inline instead of surfacing
# bare labels ("Option B") the operator cannot disambiguate.
_USER_CHAT_FIRST_TURN_NOTE = (
    "[System note: this is a side-chat with the operator. "
    "Your instructions may define a menu of options (Option A, Option B, …). "
    "The operator sees ONLY what you write in this panel — they do NOT see "
    "your instructions.  Every time you reference an option label you MUST "
    "restate its full definition inline so the operator can understand it "
    "without switching context.  For example, instead of writing "
    '"Option B is the right call," write '
    '"Option B (phased: cleanup now, warning-first gate, fail-closed only '
    'after auto-mail migrates) is the right call."  This applies to every '
    "turn — the initial recommendation and any follow-up confirmation-gate "
    "turns.  If you present multiple options, show ALL of them with their "
    "definitions so the operator can compare.]"
)


# Consecutive stale-worker resume attempts before the subsession is closed.


# Phrases that, when they appear at the start of a periodic reply,
# indicate the agent found nothing to report.  Kept broad enough to
# catch common LLM paraphrasing of "nothing changed" without being so
# broad that it swallows real status updates.
_NO_CHANGE_PHRASES: tuple[str, ...] = (
    "NO CHANGE",
    "NO CHANGES",
    "NOTHING CHANGED",
    "NOTHING HAS CHANGED",
    "NO UPDATES",
    "UNCHANGED",
    "NO NEW",
    "EVERYTHING IS THE SAME",
    "ALL QUIET",
    "STATUS UNCHANGED",
    "NO SIGNIFICANT CHANGE",
    "NO MEANINGFUL CHANGE",
)


def _format_duration(seconds: float) -> str:
    """Return a human-readable duration string for *seconds*."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} min"
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def _is_no_change(reply: str) -> bool:
    """Whether *reply* is the periodic no-change sentinel or a common paraphrase.

    The LLM sometimes returns a paraphrase instead of the exact sentinel.
    """
    cleaned = reply.strip().upper()
    if cleaned.startswith(_NO_CHANGE_SENTINEL):
        return True
    return cleaned.startswith(_NO_CHANGE_PHRASES)


def _is_duplicate_reply(reply: str, previous: str | None) -> bool:
    """Whether *reply* is identical to the previous run's reply.

    Strips and case-folds before comparing — suppresses repeated verbatim output.
    """
    if previous is None:
        return False
    return reply.strip().casefold() == previous.strip().casefold()


def _is_ticket_pre_authorized(
    ticket_id: str,
    patterns: list[str],
) -> bool:
    """Return ``True`` if *ticket_id* matches any glob pattern in *patterns*.

    Uses :func:`fnmatch.fnmatch` for case-sensitive glob matching.
    An empty *patterns* list always returns ``False``.
    """
    if not patterns:
        return False
    if not ticket_id:
        return False
    return any(fnmatch.fnmatch(ticket_id, p) for p in patterns)


@dataclass(frozen=True)
class SubsessionContext:
    """Identity captured lexically in agent tool closures.

    ``subsession_id is None`` means the agent IS the main chat agent
    (depth 0); its spawned children then have ``parent_id=None`` and
    their summaries deliver straight to the owning chat session.
    """

    owner_session_id: str
    subsession_id: str | None
    depth: int


@dataclass
class CloseState:
    """Mutable close-request holder shared between a worker and its agent.

    The ``complete_subsession`` tool sets :attr:`requested` (and the
    summary); the worker checks it after every turn.

    When the tool itself already delivered the summary (e.g. to survive
    a race with an external close), :attr:`delivery_done` is set to
    ``True`` so the worker does not deliver a second time.
    """

    requested: bool = False
    summary: str | None = None
    delivery_done: bool = False


@dataclass
class SubsessionEnv:
    """Shared runtime dependencies for spawning and running subsessions.

    ``agent_factory(settings, model_level, ctx, close_state)`` must build
    a fully-tooled agent for the subsession: the standard tool suite plus
    the depth-aware subsession tools (spawn/message/close/list when depth
    allows, ``complete_subsession`` always).
    """

    settings: Settings
    registry: SubsessionRegistry
    delivery: ParentDelivery
    conversation_store: ConversationStore
    agent_factory: Callable[[Settings, int, SubsessionContext, CloseState], ChatAgent]
    event_sink: EventSink | None = None
    # Strong refs to worker tasks spawned via spawn_subsession (belt and
    # braces alongside the registry's _running map).
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)


def spawn_subsession(
    *,
    env: SubsessionEnv,
    kind: SubsessionKind,
    owner_session_id: str,
    parent_id: str | None,
    depth: int,
    title: str,
    prompt: str,
    model_level: int,
    interval_seconds: float | None = None,
    include_previous_result: bool = False,
    max_runs: int | None = None,
    inherit_context: bool = False,
    sub_id: str | None = None,
    runs: int = 0,
    completed_runs: set[int] | None = None,
    turn_history: list[tuple[str, str]] | None = None,
    checkpoint: dict[str, object] | None = None,
    dedup_key: str | None = None,
    retry_count: int = 0,
    event_timeout_seconds: float | None = None,
) -> str:
    """Validate, register, and launch a subsession worker; return its id.

    Raises :class:`SubsessionCapacityError`, :class:`SubsessionDepthError`,
    :class:`SubsessionLevelError`, or :class:`SubsessionIntervalError` on
    invalid requests — the tool layer maps these to polite refusals.

    Idempotent: when *sub_id* is given and already registered (e.g. a
    duplicate resume), the existing worker is left alone and the id is
    returned immediately — no second worker is launched.

    *dedup_key* is an optional deduplication key.  When set and an
    active subsession with the same key already exists (of any kind),
    returns the existing subsession's id instead of launching a
    duplicate — this prevents a single root-cause event (e.g. filing
    the same ticket twice, or an ``asyncio.run`` crash affecting
    multiple ticket monitors) from spawning redundant workers.
    """
    # Idempotency guard: if the subsession already exists (duplicate
    # spawn / resume race), return the existing id without launching
    # a second worker.  Must precede validation so a duplicate resume
    # never fails on capacity / depth / level checks.
    if sub_id is not None and env.registry.get(sub_id) is not None:
        return sub_id

    # Deduplication guard: when a subsession with a dedup_key already
    # exists and is active, return its id instead of spawning a duplicate.
    if dedup_key is not None:
        existing_id = env.registry.is_dedup_key_active(dedup_key)
        if existing_id is not None:
            return existing_id
        # Cross-reference: an existing PERIODIC monitor may have been
        # spawned without a dedup_key but recorded the watched ticket_id
        # in its checkpoint after the first run.  Scan for that match.
        if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
            cp_match = env.registry.find_active_periodic_by_ticket_id(dedup_key)
            if cp_match is not None:
                return cp_match

    cfg = env.settings.subsessions
    if env.registry.count_active() >= cfg.max_concurrent:
        raise SubsessionCapacityError(
            f"subsession capacity reached ({cfg.max_concurrent} active)"
        )
    if depth > cfg.max_depth:
        raise SubsessionDepthError(
            f"maximum subsession nesting depth is {cfg.max_depth}"
        )
    _validate_model_level(env.settings, model_level)
    if parent_id is not None:
        parent = env.registry.get(parent_id)
        if parent is not None and parent.kind in (
            SubsessionKind.PERIODIC,
            SubsessionKind.WAIT_FOR_EVENT,
        ):
            if kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT):
                logger.warning(
                    "Subsession %s: periodic/wait_for_event spawn of nested "
                    "periodic/wait_for_event child rejected (kind=%s).",
                    parent_id,
                    kind.value,
                )
                raise SubsessionPeriodicSpawnError(
                    "periodic and wait_for_event subsessions cannot spawn "
                    "periodic or wait_for_event children."
                )
            if kind is SubsessionKind.ON_CLOSE:
                logger.warning(
                    "Subsession %s: periodic spawn of on_close child "
                    "rejected (kind=%s).",
                    parent_id,
                    kind.value,
                )
                raise SubsessionPeriodicSpawnError(
                    "periodic subsessions cannot spawn on_close children"
                )
    if kind is SubsessionKind.USER_CHAT and parent_id is not None:
        parent = env.registry.get(parent_id)
        if parent is not None and parent.kind is SubsessionKind.USER_CHAT:
            raise SubsessionUserChatSpawnError(
                "user_chat subsessions cannot spawn user_chat children"
            )
    if kind is SubsessionKind.PERIODIC:
        if interval_seconds is None or interval_seconds < cfg.min_interval_seconds:
            raise SubsessionIntervalError(
                f"periodic interval must be >= {cfg.min_interval_seconds} seconds"
            )
    elif kind is SubsessionKind.WAIT_FOR_EVENT:
        interval_seconds = None
    else:
        interval_seconds = None

    if inherit_context and parent_id is not None:
        prompt = _build_ancestor_context(env.registry, parent_id) + prompt

    try:
        info = env.registry.create(
            kind=kind,
            owner_session_id=owner_session_id,
            parent_id=parent_id,
            depth=depth,
            title=title,
            prompt=prompt,
            model_level=model_level,
            interval_seconds=interval_seconds,
            include_previous_result=include_previous_result,
            max_runs=max_runs,
            sub_id=sub_id,
            runs=runs,
            completed_runs=completed_runs,
            turn_history=turn_history,
            checkpoint=checkpoint,
            dedup_key=dedup_key,
            retry_count=retry_count,
            event_timeout_seconds=event_timeout_seconds,
        )
    except SubsessionDedupError as exc:
        logger.info(
            "spawn_subsession: dedup_key %r already covered by subsession %s; "
            "returning existing id (no new worker launched).",
            dedup_key,
            exc.existing_id,
        )
        return exc.existing_id
    # spawn_subsession runs inside the parent agent's turn, so a plain
    # create_task would snapshot that turn's context — including the active
    # OTEL span — and every span the subsession opens would nest inside the
    # owner session's Langfuse trace instead of forming its own (observed
    # 2026-07-11: subsession generations invisible as traces, grouped under
    # the owner's session). An empty Context() makes the worker's runs trace
    # roots, grouped under the subsession's own session id by langfuse_session.
    task = asyncio.create_task(
        _subsession_worker(env, info.id), context=contextvars.Context()
    )
    env.registry.attach_task(info.id, task)
    env._tasks.add(task)
    task.add_done_callback(env._tasks.discard)
    return info.id


def _validate_model_level(settings: Settings, model_level: int) -> None:
    """Reject invalid levels and key-bearing levels without a key."""
    from robotsix_chat.config import VALID_MODEL_LEVELS, level_needs_api_key

    if model_level not in VALID_MODEL_LEVELS:
        raise SubsessionLevelError(
            f"model_level must be one of {sorted(VALID_MODEL_LEVELS)}"
        )
    if (
        level_needs_api_key(model_level)
        and not settings.llmio_api_key.get_secret_value()
    ):
        raise SubsessionLevelError(
            f"model level {model_level} needs an API key which is not "
            "configured — use level 3 or 4"
        )


# Character budget for the ancestor-context block prepended to a nested
# child's prompt when ``inherit_context=True``.  The budget covers the
# block header plus each ancestor (title + first 300 chars of its prompt);
# ancestors beyond the budget are silently dropped so the block never
# overwhelms the child's own instructions.
_MAX_ANCESTOR_CONTEXT_CHARS = 2000


def _build_ancestor_context(registry: SubsessionRegistry, parent_id: str) -> str:
    """Walk up the parent chain and build a compact context block.

    Returns a string of the form::

        # Ancestor context (inherited from the subsession tree above you)

        ## ancestor-1 title
        ancestor-1 prompt summary …

        ## ancestor-2 title
        ...

    or an empty string when the parent chain is unreachable.
    """
    ancestors: list[SubsessionInfo] = []
    current_id: str | None = parent_id
    while current_id is not None:
        info = registry.get(current_id)
        if info is None:
            break
        ancestors.append(info)
        current_id = info.parent_id
    if not ancestors:
        return ""

    # Build from root downward (reverse the walk-up order).
    ancestors.reverse()
    parts: list[str] = [
        "# Ancestor context (inherited from the subsession tree above you)\n"
    ]
    budget = _MAX_ANCESTOR_CONTEXT_CHARS - len(parts[0])
    for info in ancestors:
        snippet = info.prompt[:300]
        entry = f"## {info.title}\n{snippet}"
        if len(entry) > budget:
            break
        parts.append(entry)
        budget -= len(entry) + 1  # +1 for the blank line separator
    if not parts[1:]:  # only the header, no actual ancestor entries
        return ""
    return "\n\n".join(parts) + "\n\n"


def _render_turn_input(messages: list[InboxMessage]) -> str:
    """Merge an inbox batch into one turn input, labelled by role."""
    if len(messages) == 1:
        return messages[0].text
    return "\n\n".join(f"[{m.role}] {m.text}" for m in messages)


def _build_periodic_input(
    info: SubsessionInfo,
    previous_result: str | None,
    steering: list[InboxMessage],
    pre_authorized_patterns: list[str] | None = None,
) -> str:
    """Compose one periodic tick's turn input."""
    parts = [info.prompt]
    if info.include_previous_result and previous_result is not None:
        parts.append(f"Previous run result:\n{previous_result}")
    if steering:
        parts.append(
            "New instructions received since the last run:\n"
            + _render_turn_input(steering)
        )
    parts.append(
        "You are a periodic monitor — being spawned directly from "
        "a conversation as a periodic subsession is the standard, "
        "fully supported workflow for ticket monitors.  You are "
        "operating exactly as designed.\n\n"
        "CRITICAL — reporting contract: your replies are NOT delivered "
        "to the parent conversation.  The only way to communicate with "
        "the parent is by calling complete_subsession(summary).  "
        "Intermediate progress — state transitions, status updates, "
        "observations, commentary — stays inside this subsession and is "
        "never seen by the parent.  Call complete_subsession ONLY when:\n"
        "  1. A final, verified terminal state is reached (ticket done, "
        "merged, closed), or\n"
        "  2. User intervention is required (ticket blocked on a decision, "
        "escalation needed, unrecoverable failure).\n"
        "For all other runs — including state transitions that do not "
        f"reach a terminal state — reply {_NO_CHANGE_SENTINEL} if nothing "
        "changed, or reply with a concise acknowledgment if something did "
        "change (the parent will not see it, but the transcript records "
        "what you observed).\n\n"
        "CRITICAL — summary formatting: when you call complete_subsession, "
        "your summary will be shown directly to a human operator.  Write "
        "in plain, user-facing language — omit ALL internal technical "
        "details.  Never include block IDs, event numbers, state machine "
        "transitions, spawn counters, internal timeout values, stack "
        "traces, or raw API response fragments.  State the actionable "
        "conclusion first, then any context the operator needs.  For "
        "example, instead of 'event 35 triggered stall guard escalation "
        "after spawn counter reset at block a3f2, history events 20-35,' "
        "write 'Publishing workflow stalled because the ticket scope was "
        "too broad — suggest splitting into a canary-only ticket.'  The "
        "operator should understand the outcome without ever seeing an "
        "internal identifier.\n\n"
        "CRITICAL — strict verify-first policy: you are a read-only "
        "monitor.  You MUST NOT infer, guess, or fabricate any state "
        "change or outcome.  Before reporting ANY state change, transition, "
        "or terminal outcome in a complete_subsession summary, you MUST do "
        "a live GET of the ticket from the board API (e.g. fetch the ticket "
        "endpoint, re-read the ticket description and comments) and compare "
        "the live response against the previously verified state from the "
        "prior run's result.  Only report what the live API returns — never "
        "trust a state you only recall from an earlier turn or infer from "
        "conversation context.  A state transition that happened between "
        "polls (e.g. draft → ready → in_progress) MUST be detected from the "
        "live query, not from your memory.  If the live API response "
        "conflicts with your recollection, the live API response is "
        "authoritative — report that, and discard the recollection.\n\n"
        "Tool fallback: use the component_request tool to fetch ticket "
        "state from the board API.  If component_request is not among "
        "your tools, use the ticket_poll tool instead — it queries the "
        "same board API directly.  If neither tool is available, call "
        "complete_subsession with a summary recommending the monitor be "
        "paused — do not silently loop.\n\n"
        f"Reply with the single word {_NO_CHANGE_SENTINEL} — and nothing "
        "else, no punctuation, no commentary — only if genuinely nothing "
        "changed since the previous run: the live board state is identical "
        "to the prior run's observed state. If any state transition occurred "
        "(e.g. draft → implement_complete, in_progress → done, ready → "
        "in_progress) but the ticket has NOT reached a terminal state, reply "
        "with a concise acknowledgment of the change (the parent will not "
        "see this — it is for the transcript only).  DO NOT reply NO_CHANGE "
        "when a transition occurred.\n\n"
    )
    # Inject the PRE-AUTHORIZED instruction BEFORE the
    # decision-blocked paragraph so it has priority — a monitor that
    # sees both must follow the pre-authorized directive.
    if pre_authorized_patterns:
        ticket_id_raw = info.checkpoint.get("ticket_id") if info.checkpoint else None
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        # Fall back to dedup_key when the checkpoint has not yet recorded
        # the ticket_id — the dedup_key for ticket monitors is always the
        # ticket id, so it is authoritative even on the first run.
        if not ticket_id and info.dedup_key:
            ticket_id = info.dedup_key
        if ticket_id and _is_ticket_pre_authorized(ticket_id, pre_authorized_patterns):
            parts.append(
                "PRE-AUTHORIZED TICKET: this ticket has been pre-authorized "
                "under a standing operator directive.  The "
                "human_issue_approval gate does NOT apply — do not treat "
                "this ticket as decision-blocked.  Continue monitoring "
                "normally as if the approval were already granted.\n\n"
            )
    parts.append(
        "Decision-blocked tickets: when the monitored ticket is awaiting an "
        "operator decision — stuck in human_issue_approval, waiting on an "
        '"Option A or B?" choice, or otherwise blocked on a human '
        "direction — do NOT silently reply NO_CHANGE run after run.  "
        "Instead, call complete_subsession with a summary that includes a "
        "CONCRETE RECOMMENDATION: state whether you recommend approving or "
        "closing the ticket and why (e.g. 'I recommend approving — this "
        "is a standard pre-authorized rollout step' or 'I recommend closing "
        "— the change is already covered by ticket X').  Explain what "
        "decision is needed and set expectations: mention that the system "
        "will auto-escalate after the human_approval_timeout window if no "
        "decision is made.  This surfaces the blocker with actionable "
        "guidance so the operator can act on it rather than waiting for "
        "the auto-stop timeout.\n\n"
    )
    parts.append(
        "Terminal-state double-check + loop guard: before calling "
        "complete_subsession for a done or closed ticket, you MUST verify "
        "from three independent sources — (1) a live GET of the ticket "
        "endpoint confirming the terminal state, (2) a check of the PR/MR "
        "endpoint (e.g. the ticket's linked PRs or the merge API) confirming "
        "merge status, and (3) a check of the most recent CI workflow run "
        "for the affected pipeline (e.g. the 'Publish Docker image' workflow "
        "or the repo's primary deploy workflow).  "
        "Do NOT claim a PR was created, merged, or auto-merged unless you "
        "have confirmed it via the PR API — a terminal ticket state alone "
        "does not prove a PR exists.  Your complete_subsession summary MUST "
        "state which sources you checked and what each returned.  If a PR "
        "was merged, say so with the PR number; if no PR was involved, say "
        "'closed without a PR'; if the PR API is unreachable, say 'terminal "
        "state confirmed via ticket API; PR status could not be verified'.  "
        "\n\n"
        "LOOP GUARD — CI workflow verification (source 3): after a ticket "
        "closes, query the GitHub Actions API (via component_request or "
        "the equivalent GitHub API tool) for the most recent run of the "
        "repo's primary publish/deploy workflow.  The complete_subsession "
        "tool has a PROGRAMMATIC GATE: it will REJECT any summary that "
        "does not mention 'CI workflow', 'workflow run', 'pipeline', "
        "'GitHub Actions', 'publish', 'deploy workflow', or 'could not be "
        "verified'.  You must include at least one of these phrases in "
        "your summary to pass the gate.  Simply stating the ticket is "
        "closed without CI evidence will be rejected.\n\n"
        "If the workflow run failed or is still in progress with failures "
        "on prior runs, do NOT call complete_subsession with a success "
        "summary — the fix did not actually resolve the pipeline failure.  "
        "Instead:\n"
        "  - If the workflow failed: call complete_subsession with a "
        "summary that INCLUDES the workflow failure details (run id, "
        "failure reason, and log excerpt if available).  The summary must "
        "make clear that the ticket was closed but the CI pipeline is "
        "still failing — this breaks the redraft loop.  Then, AFTER "
        "complete_subsession returns, call spawn_subsession to file a "
        "new diagnostic ticket with the workflow failure details so the "
        "operator can see the pipeline is still broken.\n"
        "  - If the workflow API is unreachable: try at least twice with "
        "a 5-second pause between attempts.  If still unreachable, call "
        "complete_subsession with a summary stating 'terminal state "
        "confirmed via ticket API; CI workflow status could not be "
        "verified' — do NOT silently skip the check.\n"
        "  - If the workflow passed: proceed with complete_subsession "
        "normally (the fix is confirmed effective).\n"
        "Call complete_subsession only after all three checks are complete."
    )
    parts.append(
        "CRITICAL — checkpoint PR tracking: whenever you detect or create "
        "a PR associated with the monitored ticket (via open_direct_repo_pr "
        "or by querying the GitHub API), store the PR number and repository "
        "in this subsession's checkpoint using set_checkpoint with "
        "'pr_number' (int) and 'repo_full_name' (str, e.g. "
        "'owner/repo').  Include the existing checkpoint fields "
        "(ticket_id, last_known_state, human_approval_since) alongside "
        "the new PR fields — the checkpoint is replaced wholesale.  "
        "This enables the background watcher to detect PR merges and "
        "auto-resume the monitor after a merge event, even when the "
        "board ticket state has not yet been updated.  If you previously "
        "created a PR and it was later merged or closed, update the "
        "checkpoint to remove stale PR information."
    )
    return "\n\n".join(parts)


def _is_github_rate_limit_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a GitHub API rate-limit error (403/429).

    Inspects the exception's ``__cause__`` and ``__context__`` chain
    for ``RuntimeError`` messages matching the pattern emitted by
    :class:`~robotsix_chat.repo.direct.client.DirectRepoClient`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        exc_id = id(current)
        if exc_id in seen:
            break
        seen.add(exc_id)
        if isinstance(current, RuntimeError):
            msg = str(current)
            if "GitHub API" in msg and (
                "error 429" in msg
                or ("error 403" in msg and "rate limit" in msg.lower())
            ):
                return True
        current = current.__cause__ or current.__context__
    return False


async def _run_turn_with_transient_retry(
    env: SubsessionEnv,
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    info: SubsessionInfo,
) -> str:
    """Run one agent turn, retrying on transient API errors for periodic subsessions.

    For PERIODIC subsessions only: transient errors (e.g. OpenRouter
    upstream hiccups, GitHub API rate-limit 403/429) are retried with
    exponential backoff.  When all retries are exhausted the function
    raises :class:`_TransientExhaustedError` so the worker loop can skip
    the cycle gracefully instead of permanently failing the subsession.

    For TASK / USER_CHAT subsessions the error propagates unchanged —
    the outer handler will fail the subsession.
    """
    settings = env.settings.subsessions
    max_retries = settings.transient_error_max_retries
    base = settings.transient_error_backoff_base
    cap = settings.transient_error_backoff_cap

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _run_turn_with_timeout(
                env, agent, turn_input, history, sub_id, info
            )
        except _RunTimeoutError:
            raise  # timeout is handled separately; never retried
        except Exception as exc:
            last_exc = exc
            is_periodic = info.kind is SubsessionKind.PERIODIC
            is_transient = is_openrouter_transient(exc) or (
                is_periodic and _is_github_rate_limit_error(exc)
            )
            if not is_periodic or not is_transient:
                raise

            if attempt < max_retries:
                delay = min(base * (2**attempt), cap)
                logger.warning(
                    "Subsession %s run %d: transient error on attempt %d/%d — "
                    "retrying in %.1fs. Error: %s",
                    sub_id,
                    info.runs + 1,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                continue

    # All retries exhausted for a periodic subsession — raise a sentinel
    # so the worker loop can skip this cycle instead of failing permanently.
    logger.error(
        "Subsession %s run %d: all %d transient-error retries exhausted "
        "(last error: %s) — skipping this cycle.",
        sub_id,
        info.runs + 1,
        max_retries + 1,
        last_exc,
    )
    raise _TransientExhaustedError(
        f"transient error persisted across {max_retries + 1} attempts"
    ) from last_exc


class _TransientExhaustedError(Exception):
    """Sentinel raised when all transient-error retries are exhausted.

    Caught by the worker loop for periodic subsessions to skip the cycle
    gracefully rather than permanently failing the subsession.
    """


async def _run_turn(
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    *,
    trace_metadata: dict[str, str] | None = None,
) -> str:
    """Run one agent turn and return the reply text."""
    parts = [
        chunk
        async for chunk in agent.stream(
            turn_input,
            history=history[-_MAX_WORKER_HISTORY_TURNS:] or None,
            session_id=sub_id,
            client_id=sub_id,
            trace_metadata=trace_metadata,
        )
    ]
    return "".join(parts)


_RUN_TIMEOUT_GRACE = 5.0
"""Seconds of grace added to the configured run timeout for the
asyncio.timeout context so the warning + status update have time to
execute before the CancelledError propagates."""


async def _run_turn_with_timeout(
    env: SubsessionEnv,
    agent: ChatAgent,
    turn_input: str,
    history: list[tuple[str, str]],
    sub_id: str,
    info: SubsessionInfo,
) -> str:
    """Run one agent turn with a hard timeout guard.

    On timeout the run is marked failed for TASK/USER_CHAT kinds, or the
    schedule continues with the failure recorded for PERIODIC kinds.
    """
    timeout = env.settings.subsessions.run_timeout_seconds
    try:
        async with asyncio.timeout(timeout + _RUN_TIMEOUT_GRACE):
            return await _run_turn(
                agent,
                turn_input,
                history,
                sub_id,
                trace_metadata={
                    "owner_session_id": info.owner_session_id,
                    "parent_session_id": info.parent_id or info.owner_session_id,
                },
            )
    except TimeoutError:
        logger.warning(
            "Subsession %s run timed out after %.0fs; marking run as failed",
            sub_id,
            timeout,
        )
        raise _RunTimeoutError(
            f"subsession run exceeded {timeout:.0f}s timeout"
        ) from None


class _RunTimeoutError(Exception):
    """Raised when a single subsession turn exceeds the run timeout.

    Internal sentinel — caught by the worker loop to trigger kind-specific
    failure handling without conflating with other CancelledError sources.
    """


async def _run_task_turn(
    env: SubsessionEnv, sub_id: str, reply: str
) -> list[InboxMessage]:
    """Handle TASK post-turn: drain inbox; return pending messages or close.

    Returns a non-empty list if steering messages arrived mid-turn
    (the worker should continue), or an empty list after closing the
    subsession (the worker should stop).
    """
    pending = env.registry.drain_inbox(sub_id)
    if pending:
        return pending  # a steering message arrived mid-turn
    closed = env.registry.mark_closed(
        sub_id, summary=reply, reason="completed", closed_by="agent"
    )
    if closed is not None:
        await env.delivery.deliver_summary(closed, reply, "completed")
    return []


async def _run_user_chat_turn(env: SubsessionEnv, sub_id: str) -> list[InboxMessage]:
    """Handle USER_CHAT post-turn: wait for inbox, drain, return pending."""
    env.registry.set_status(sub_id, SubsessionStatus.WAITING)
    await env.registry.wait_for_inbox(sub_id, timeout=None)
    return env.registry.drain_inbox(sub_id)


# How long a paused monitor blocks on wait_for_inbox before looping —
# the watcher sends an inbox message immediately when it detects a
# ticket-state change, so the timeout is only a safety net.
_PAUSED_WAIT_TIMEOUT_SECONDS: float = 300.0


async def _query_mill_ticket_state(
    board_url: str, ticket_id: str, sub_id: str
) -> str | None:
    """Return the current state string for *ticket_id*, or ``None`` on error.

    A lightweight copy of :func:`watcher._query_ticket_state` used by
    :func:`_paused_wait_loop` for per-monitor long-polling — avoids a
    circular import from the watcher module.
    """
    try:
        base = httpx.URL(board_url.rstrip("/"))
        ticket_url = base.copy_with(path=f"/tickets/{ticket_id}")
    except Exception:
        logger.exception("Could not construct ticket URL for subsession %s", sub_id)
        return None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(str(ticket_url))
            response.raise_for_status()
            ticket_data: dict[str, object] = response.json()
    except httpx.HTTPStatusError as exc:
        logger.debug(
            "Long-poll: mill returned %d for ticket %s (subsession %s)",
            exc.response.status_code,
            ticket_id,
            sub_id,
        )
        return None
    except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
        logger.debug(
            "Long-poll: mill unreachable for ticket %s (subsession %s): %s",
            ticket_id,
            sub_id,
            exc,
        )
        return None
    except Exception:
        logger.exception(
            "Long-poll: unexpected error querying mill for ticket %s (subsession %s)",
            ticket_id,
            sub_id,
        )
        return None

    state = ticket_data.get("state")
    return (
        state if isinstance(state, str) else str(state) if state is not None else None
    )


async def _paused_wait_loop(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Block until a ticket-state-change signal arrives or the worker is cancelled.

    Called after ``mark_paused`` — the worker stays alive and waits on
    the inbox event (watcher-sent wake messages) AND polls the mill API
    directly at a shorter long-poll interval.  When either mechanism
    detects a state change, this returns ``(pending, previous_result,
    consecutive_no_change)`` so the worker can resume its normal periodic
    loop.  Returns ``None`` when the subsession was externally closed
    while paused.
    """
    registry = env.registry

    # -- long-poll setup -------------------------------------------------
    checkpoint = info.checkpoint or {}
    ticket_id_raw = checkpoint.get("ticket_id")
    ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
    last_known_state = checkpoint.get("last_known_state")
    last_known_str: str | None = (
        (
            last_known_state
            if isinstance(last_known_state, str)
            else str(last_known_state)
        )
        if last_known_state is not None
        else None
    )

    direct_repo = getattr(env.settings, "direct_repo", None)
    board_url: str = (
        getattr(direct_repo, "board_api_base_url", "")
        if direct_repo is not None
        else ""
    )
    long_poll_interval: float = getattr(
        env.settings.subsessions,
        "paused_monitor_long_poll_interval_seconds",
        15.0,
    )
    can_long_poll = bool(
        board_url and ticket_id and last_known_str and long_poll_interval > 0
    )
    if can_long_poll:
        logger.debug(
            "Subsession %s: long-poll enabled for ticket %s "
            "(interval=%.0fs, last_known=%s).",
            sub_id,
            ticket_id,
            long_poll_interval,
            last_known_str,
        )
    # --------------------------------------------------------------------

    async def _try_resume(
        reason: str,
        pending: list[InboxMessage] | None = None,
    ) -> tuple[list[InboxMessage], str | None, int] | None:
        """Resume the subsession and publish an SSE notification.

        *pending* — inbox messages to hand to the worker on its next
        turn (e.g. the parent message that triggered the resume).
        When ``None`` (long-poll wake), defaults to an empty list.
        """
        resumed = registry.resume(sub_id)
        if resumed is None:
            return None
        if env.event_sink is not None:
            env.event_sink.publish(
                info.owner_session_id,
                {
                    "type": SSE_NOTIFICATION_TYPE,
                    "title": f"Monitor resumed: {info.title}",
                    "body": (
                        f"Monitor {sub_id[:8]} tracking ticket {ticket_id} "
                        f"resumed after {reason}."
                    ),
                    "urgency": "low",
                    "link": ticket_id,
                },
            )
        if pending is None:
            pending = []
        return pending, previous_result, consecutive_no_change

    while True:
        timeout = long_poll_interval if can_long_poll else _PAUSED_WAIT_TIMEOUT_SECONDS
        woke = await registry.wait_for_inbox(sub_id, timeout=timeout)

        # Verify the subsession is still paused.
        current = registry.get(sub_id)
        if current is None or current.status is not SubsessionStatus.PAUSED:
            return None

        if woke:
            # ----------- inbox wake: any message resumes -------------
            # The watcher sends system messages on ticket-state change;
            # the parent agent sends parent messages via
            # ``message_subsession`` to manually resume.  Both paths
            # wake the monitor and hand the message through so the
            # subsession agent sees it on its next turn.
            messages = registry.drain_inbox(sub_id)
            if messages:
                logger.info(
                    "Subsession %s: resume signal received via inbox — resuming.",
                    sub_id,
                )
                return await _try_resume("inbox message", pending=messages)

            # Spurious wake (event set but inbox empty) — loop and wait
            # again.
            logger.debug(
                "Subsession %s: spurious inbox wake; continuing wait.",
                sub_id,
            )
        else:
            # ----------- timeout: long-poll the mill directly -------------
            if not can_long_poll:
                # Safety-net timeout with long-poll disabled — just loop.
                continue

            if not ticket_id or not last_known_str:
                # Defensive: can_long_poll should guarantee these are set,
                # but if something mutated the checkpoint, fall through.
                continue

            current_state = await _query_mill_ticket_state(board_url, ticket_id, sub_id)
            if current_state is not None and current_state != last_known_str:
                logger.info(
                    "Subsession %s: ticket %s state changed from '%s' to '%s' "
                    "(detected via long-poll) — resuming.",
                    sub_id,
                    ticket_id,
                    last_known_str,
                    current_state,
                )
                return await _try_resume("ticket state change (long-poll)")

            # State unchanged — loop back and wait again.
            logger.debug(
                "Subsession %s: ticket %s still '%s' (long-poll) — continuing wait.",
                sub_id,
                ticket_id,
                current_state,
            )


def _build_wait_for_event_input(
    info: SubsessionInfo,
    previous_result: str | None,
    steering: list[InboxMessage],
    pre_authorized_patterns: list[str] | None = None,
) -> str:
    """Compose one event-driven monitor turn's input."""
    parts = [info.prompt]
    if info.include_previous_result and previous_result is not None:
        parts.append(f"Previous run result:\n{previous_result}")
    if steering:
        parts.append(
            "Event(s) received since the last run:\n" + _render_turn_input(steering)
        )
    parts.append(
        "You are an event-driven monitor — you wake only when a mill "
        "ticket state-change event arrives or when a safety-net timeout "
        "fires.  You are operating exactly as designed.\n\n"
        "CRITICAL — reporting contract: your replies are NOT delivered "
        "to the parent conversation.  The only way to communicate with "
        "the parent is by calling complete_subsession(summary).  "
        "Intermediate progress — state transitions, status updates, "
        "observations, commentary — stays inside this subsession and is "
        "never seen by the parent.  Call complete_subsession ONLY when:\n"
        "  1. A final, verified terminal state is reached (ticket done, "
        "merged, closed), or\n"
        "  2. User intervention is required (ticket blocked on a decision, "
        "escalation needed, unrecoverable failure).\n"
        "For all other runs — including state transitions that do not "
        f"reach a terminal state — reply {_NO_CHANGE_SENTINEL} if nothing "
        "changed, or reply with a concise acknowledgment if something did "
        "change (the parent will not see it, but the transcript records "
        "what you observed).\n\n"
        "CRITICAL — summary formatting: when you call complete_subsession, "
        "your summary will be shown directly to a human operator.  Write "
        "in plain, user-facing language — omit ALL internal technical "
        "details.  Never include block IDs, event numbers, state machine "
        "transitions, spawn counters, internal timeout values, stack "
        "traces, or raw API response fragments.  State the actionable "
        "conclusion first, then any context the operator needs.  For "
        "example, instead of 'event 35 triggered stall guard escalation "
        "after spawn counter reset at block a3f2, history events 20-35,' "
        "write 'Publishing workflow stalled because the ticket scope was "
        "too broad — suggest splitting into a canary-only ticket.'  The "
        "operator should understand the outcome without ever seeing an "
        "internal identifier.\n\n"
        "CRITICAL — strict verify-first policy: you are a read-only "
        "monitor.  You MUST NOT infer, guess, or fabricate any state "
        "change or outcome.  Before reporting ANY state change, transition, "
        "or terminal outcome in a complete_subsession summary, you MUST do "
        "a live GET of the ticket from the board API (e.g. fetch the ticket "
        "endpoint, re-read the ticket description and comments) and compare "
        "the live response against the previously verified state from the "
        "prior run's result.  Only report what the live API returns — never "
        "trust a state you only recall from an earlier turn or infer from "
        "conversation context.  A state transition that happened between "
        "polls (e.g. draft → ready → in_progress) MUST be detected from the "
        "live query, not from your memory.  If the live API response "
        "conflicts with your recollection, the live API response is "
        "authoritative — report that, and discard the recollection.\n\n"
        "Tool fallback: use the component_request tool to fetch ticket "
        "state from the board API.  If component_request is not among "
        "your tools, use the ticket_poll tool instead — it queries the "
        "same board API directly.  If neither tool is available, call "
        "complete_subsession with a summary recommending the monitor be "
        "paused — do not silently loop.\n\n"
        f"Reply with the single word {_NO_CHANGE_SENTINEL} — and nothing "
        "else, no punctuation, no commentary — only if genuinely nothing "
        "changed since the previous run: the live board state is identical "
        "to the prior run's observed state. If any state transition occurred "
        "(e.g. draft → implement_complete, in_progress → done, ready → "
        "in_progress) but the ticket has NOT reached a terminal state, reply "
        "with a concise acknowledgment of the change (the parent will not "
        "see this — it is for the transcript only).  DO NOT reply NO_CHANGE "
        "when a transition occurred.\n\n"
    )
    # Inject the PRE-AUTHORIZED instruction BEFORE the
    # decision-blocked paragraph so it has priority — a monitor that
    # sees both must follow the pre-authorized directive.
    if pre_authorized_patterns:
        ticket_id_raw = info.checkpoint.get("ticket_id") if info.checkpoint else None
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        # Fall back to dedup_key when the checkpoint has not yet recorded
        # the ticket_id — the dedup_key for ticket monitors is always the
        # ticket id, so it is authoritative even on the first run.
        if not ticket_id and info.dedup_key:
            ticket_id = info.dedup_key
        if ticket_id and _is_ticket_pre_authorized(ticket_id, pre_authorized_patterns):
            parts.append(
                "PRE-AUTHORIZED TICKET: this ticket has been pre-authorized "
                "under a standing operator directive.  The "
                "human_issue_approval gate does NOT apply — do not treat "
                "this ticket as decision-blocked.  Continue monitoring "
                "normally as if the approval were already granted.\n\n"
            )
    parts.append(
        "Decision-blocked tickets: when the monitored ticket is awaiting an "
        "operator decision — stuck in human_issue_approval, waiting on an "
        '"Option A or B?" choice, or otherwise blocked on a human '
        "direction — do NOT silently reply NO_CHANGE run after run.  "
        "Instead, call complete_subsession with a summary that includes a "
        "CONCRETE RECOMMENDATION: state whether you recommend approving or "
        "closing the ticket and why (e.g. 'I recommend approving — this "
        "is a standard pre-authorized rollout step' or 'I recommend closing "
        "— the change is already covered by ticket X').  Explain what "
        "decision is needed and set expectations: mention that the system "
        "will auto-escalate after the human_approval_timeout window if no "
        "decision is made.  This surfaces the blocker with actionable "
        "guidance so the operator can act on it rather than waiting for "
        "the auto-stop timeout.\n\n"
    )
    parts.append(
        "Terminal-state double-check + loop guard: before calling "
        "complete_subsession for a done or closed ticket, you MUST verify "
        "from three independent sources — (1) a live GET of the ticket "
        "endpoint confirming the terminal state, (2) a check of the PR/MR "
        "endpoint (e.g. the ticket's linked PRs or the merge API) confirming "
        "merge status, and (3) a check of the most recent CI workflow run "
        "for the affected pipeline (e.g. the 'Publish Docker image' workflow "
        "or the repo's primary deploy workflow).  "
        "Do NOT claim a PR was created, merged, or auto-merged unless you "
        "have confirmed it via the PR API — a terminal ticket state alone "
        "does not prove a PR exists.  Your complete_subsession summary MUST "
        "state which sources you checked and what each returned.  If a PR "
        "was merged, say so with the PR number; if no PR was involved, say "
        "'closed without a PR'; if the PR API is unreachable, say 'terminal "
        "state confirmed via ticket API; PR status could not be verified'.  "
        "\n\n"
        "LOOP GUARD — CI workflow verification (source 3): after a ticket "
        "closes, query the GitHub Actions API (via component_request or "
        "the equivalent GitHub API tool) for the most recent run of the "
        "repo's primary publish/deploy workflow.  The complete_subsession "
        "tool has a PROGRAMMATIC GATE: it will REJECT any summary that "
        "does not mention 'CI workflow', 'workflow run', 'pipeline', "
        "'GitHub Actions', 'publish', 'deploy workflow', or 'could not be "
        "verified'.  You must include at least one of these phrases in "
        "your summary to pass the gate.  Simply stating the ticket is "
        "closed without CI evidence will be rejected.\n\n"
        "If the workflow run failed or is still in progress with failures "
        "on prior runs, do NOT call complete_subsession with a success "
        "summary — the fix did not actually resolve the pipeline failure.  "
        "Instead:\n"
        "  - If the workflow failed: call complete_subsession with a "
        "summary that INCLUDES the workflow failure details (run id, "
        "failure reason, and log excerpt if available).  The summary must "
        "make clear that the ticket was closed but the CI pipeline is "
        "still failing — this breaks the redraft loop.  Then, AFTER "
        "complete_subsession returns, call spawn_subsession to file a "
        "new diagnostic ticket with the workflow failure details so the "
        "operator can see the pipeline is still broken.\n"
        "  - If the workflow API is unreachable: try at least twice with "
        "a 5-second pause between attempts.  If still unreachable, call "
        "complete_subsession with a summary stating 'terminal state "
        "confirmed via ticket API; CI workflow status could not be "
        "verified' — do NOT silently skip the check.\n"
        "  - If the workflow passed: proceed with complete_subsession "
        "normally (the fix is confirmed effective).\n"
        "Call complete_subsession only after all three checks are complete."
    )
    parts.append(
        "CRITICAL — checkpoint PR tracking: whenever you detect or create "
        "a PR associated with the monitored ticket (via open_direct_repo_pr "
        "or by querying the GitHub API), store the PR number and repository "
        "in this subsession's checkpoint using set_checkpoint with "
        "'pr_number' (int) and 'repo_full_name' (str, e.g. "
        "'owner/repo').  Include the existing checkpoint fields "
        "(ticket_id, last_known_state, human_approval_since) alongside "
        "the new PR fields — the checkpoint is replaced wholesale.  "
        "This enables the background watcher to detect PR merges and "
        "auto-resume the monitor after a merge event, even when the "
        "board ticket state has not yet been updated.  If you previously "
        "created a PR and it was later merged or closed, update the "
        "checkpoint to remove stale PR information."
    )
    return "\n\n".join(parts)


async def _event_wait_loop(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Block until a ticket-state-change event arrives or a safety-net timeout fires.

    Returns ``(pending, previous_result, consecutive_no_change)`` when the
    subsession should run a turn, or ``None`` when it was externally closed
    while waiting.
    """
    registry = env.registry
    checkpoint = info.checkpoint or {}
    ticket_id_raw = checkpoint.get("ticket_id")
    ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
    # Fall back to dedup_key when the checkpoint has not yet recorded
    # the ticket_id — the dedup_key for ticket monitors is always the
    # ticket id, so it is authoritative even on the first run.
    if not ticket_id and info.dedup_key:
        ticket_id = info.dedup_key

    if not ticket_id:
        logger.error(
            "Subsession %s: WAIT_FOR_EVENT subsession has no ticket_id in "
            "checkpoint — cannot register for events.  Closing.",
            sub_id,
        )
        closed = registry.mark_closed(
            sub_id,
            summary="No ticket_id in checkpoint — cannot wait for events.",
            reason="missing_ticket_id",
            closed_by="system",
        )
        if closed is not None:
            await env.delivery.deliver_summary(
                closed, "No ticket_id in checkpoint", "missing_ticket_id"
            )
        return None

    timeout = info.event_timeout_seconds
    if timeout is None:
        timeout = env.settings.subsessions.event_driven_timeout_seconds

    registry.register_event_waiter(sub_id, ticket_id)
    try:
        woke = await registry.wait_for_inbox(sub_id, timeout=timeout)
    finally:
        registry.unregister_event_waiter(sub_id, ticket_id)

    # Verify subsession is still active.
    current = registry.get(sub_id)
    if current is None or not current.is_active:
        return None

    if woke:
        pending = registry.drain_inbox(sub_id)
        if pending:
            return pending, previous_result, consecutive_no_change

    # Timeout — create a safety-net message so the agent runs anyway.
    pending = [
        InboxMessage(
            role="system",
            text=(
                "Safety-net timeout fired — no ticket state-change event "
                "was received.  Perform a routine check of the monitored "
                "ticket as you normally would."
            ),
            timestamp=registry.now(),
        )
    ]
    return pending, previous_result, consecutive_no_change


async def _run_wait_for_event_turn(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Handle WAIT_FOR_EVENT post-turn: update status, deliver, re-arm wait.

    Does NOT auto-pause, auto-stop, or enforce max_runs — the subsession
    runs only when events arrive.  Returns ``None`` on human_approval_timeout
    closure, or ``([], previous_result, consecutive_no_change)`` to re-arm
    the event wait.
    """
    registry = env.registry
    suppressed = _is_no_change(reply) or _is_duplicate_reply(reply, previous_result)
    consecutive_no_change = 0 if not _is_no_change(reply) else consecutive_no_change + 1
    runs = info.runs + 1
    registry.set_status(
        sub_id,
        SubsessionStatus.SLEEPING,
        runs=runs,
        last_result=reply,
    )
    if not suppressed and env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            subsession_result_frame(
                sub_id,
                info.kind.value,
                info.title,
                runs,
                reply,
                info.parent_id,
            ),
        )
    previous_result = reply

    # Human-approval timeout logic (same as _run_periodic_turn).
    checkpoint = info.checkpoint or {}
    last_known = checkpoint.get("last_known_state", "")
    if isinstance(last_known, str) and last_known.lower() == "human_issue_approval":
        patterns = env.settings.subsessions.pre_authorized_ticket_patterns
        ticket_id_raw = checkpoint.get("ticket_id")
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        pre_authorized = _is_ticket_pre_authorized(ticket_id, patterns)

        if pre_authorized and consecutive_no_change >= 1:
            logger.info(
                "Subsession %s: pre-authorized ticket %s in "
                "human_issue_approval — auto-escalating immediately.",
                sub_id,
                ticket_id,
            )
            elapsed = _format_duration(registry.now() - info.created_at)
            summary = (
                f"Pre-authorized ticket {ticket_id} entered "
                f"human_issue_approval — auto-escalating immediately "
                f"under standing operator directive "
                f"({elapsed} elapsed)."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="pre_authorized_approval",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "pre_authorized_approval"
                )
            return None

        now = registry.now()
        human_approval_since_raw = checkpoint.get("human_approval_since")
        if isinstance(human_approval_since_raw, (int, float)):
            human_approval_since = float(human_approval_since_raw)
        else:
            human_approval_since = now
            checkpoint["human_approval_since"] = now
            registry.update_checkpoint(sub_id, checkpoint)

        human_approval_timeout_s = (
            env.settings.subsessions.human_approval_timeout_seconds
        )
        if now - human_approval_since >= human_approval_timeout_s:
            logger.warning(
                "Subsession %s: auto-escalating after %.0f s in "
                "human_issue_approval state (%.0f s total elapsed).",
                sub_id,
                now - human_approval_since,
                now - info.created_at,
            )
            elapsed = _format_duration(now - info.created_at)
            stuck_for = _format_duration(now - human_approval_since)
            summary = (
                f"Ticket has been stuck at human_issue_approval for "
                f"{stuck_for} ({elapsed} total elapsed) — "
                f"auto-escalating (wall-clock timeout)."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="human_approval_timeout",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "human_approval_timeout"
                )
            return None

        human_approval_cap = env.settings.subsessions.human_approval_timeout_runs
        if consecutive_no_change >= human_approval_cap:
            logger.warning(
                "Subsession %s: auto-escalating after %d consecutive "
                "no-change runs in human_issue_approval state.",
                sub_id,
                consecutive_no_change,
            )
            elapsed = _format_duration(registry.now() - info.created_at)
            summary = (
                f"Ticket has been stuck at human_issue_approval for "
                f"{human_approval_cap} consecutive no-change runs "
                f"({elapsed} elapsed) — auto-escalating."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="human_approval_timeout",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "human_approval_timeout"
                )
            return None

    # Re-arm the wait — no auto-pause, auto-stop, or max_runs for
    # event-driven monitors.
    return [], previous_result, consecutive_no_change


async def _run_periodic_turn(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Handle PERIODIC post-turn: update status, deliver, check limits, sleep.

    Returns ``None`` when the worker should stop (max_runs / auto_stop
    triggered), or ``(pending, previous_result, consecutive_no_change)``
    to continue.
    """
    registry = env.registry
    suppressed = _is_no_change(reply) or _is_duplicate_reply(reply, previous_result)
    consecutive_no_change = 0 if not _is_no_change(reply) else consecutive_no_change + 1
    runs = info.runs + 1
    if info.interval_seconds is None:  # pragma: no cover - spawn validates
        raise RuntimeError("periodic subsession without an interval")
    registry.set_status(
        sub_id,
        SubsessionStatus.SLEEPING,
        runs=runs,
        next_run_at=registry.now() + info.interval_seconds,
        last_result=reply,
    )
    if not suppressed and env.event_sink is not None:
        env.event_sink.publish(
            info.owner_session_id,
            subsession_result_frame(
                sub_id,
                info.kind.value,
                info.title,
                runs,
                reply,
                info.parent_id,
            ),
        )
    previous_result = reply

    if info.max_runs is not None and runs >= info.max_runs:
        summary = f"Reached the {info.max_runs}-run limit. Last: {reply}"
        closed = registry.mark_closed(
            sub_id, summary=summary, reason="max_runs", closed_by="system"
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "max_runs")
        return None

    # Human-approval timeout: when the checkpoint's last_known_state is
    # human_issue_approval and the subsession has produced enough
    # consecutive NO_CHANGE runs, auto-escalate by closing with a
    # distinct reason so the parent agent can act on it.
    #
    # Pre-authorized fast-path: when the monitored ticket matches a
    # pre_authorized_ticket_patterns entry, escalate immediately on the
    # first NO_CHANGE run instead of waiting for the full timeout.
    checkpoint = info.checkpoint or {}
    last_known = checkpoint.get("last_known_state", "")
    if isinstance(last_known, str) and last_known.lower() == "human_issue_approval":
        patterns = env.settings.subsessions.pre_authorized_ticket_patterns
        ticket_id_raw = checkpoint.get("ticket_id")
        ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
        pre_authorized = _is_ticket_pre_authorized(ticket_id, patterns)

        if pre_authorized and consecutive_no_change >= 1:
            logger.info(
                "Subsession %s: pre-authorized ticket %s in "
                "human_issue_approval — auto-escalating immediately.",
                sub_id,
                ticket_id,
            )
            elapsed = _format_duration(registry.now() - info.created_at)
            summary = (
                f"Pre-authorized ticket {ticket_id} entered "
                f"human_issue_approval — auto-escalating immediately "
                f"under standing operator directive "
                f"({elapsed} elapsed)."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="pre_authorized_approval",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "pre_authorized_approval"
                )
            return None

        # Track how long the checkpoint has carried human_issue_approval
        # so we can auto-escalate on wall-clock time even when the agent
        # never emits NO_CHANGE (e.g. it follows the system prompt and
        # calls complete_subsession instead, but the call fails).
        now = registry.now()
        human_approval_since_raw = checkpoint.get("human_approval_since")
        if isinstance(human_approval_since_raw, (int, float)):
            human_approval_since = float(human_approval_since_raw)
        else:
            human_approval_since = now
            checkpoint["human_approval_since"] = now
            registry.update_checkpoint(sub_id, checkpoint)

        human_approval_timeout_s = (
            env.settings.subsessions.human_approval_timeout_seconds
        )
        if now - human_approval_since >= human_approval_timeout_s:
            logger.warning(
                "Subsession %s: auto-escalating after %.0f s in "
                "human_issue_approval state (%.0f s total elapsed).",
                sub_id,
                now - human_approval_since,
                now - info.created_at,
            )
            elapsed = _format_duration(now - info.created_at)
            stuck_for = _format_duration(now - human_approval_since)
            summary = (
                f"Ticket has been stuck at human_issue_approval for "
                f"{stuck_for} ({elapsed} total elapsed) — "
                f"auto-escalating (wall-clock timeout)."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="human_approval_timeout",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "human_approval_timeout"
                )
            return None

        human_approval_cap = env.settings.subsessions.human_approval_timeout_runs
        if consecutive_no_change >= human_approval_cap:
            logger.warning(
                "Subsession %s: auto-escalating after %d consecutive "
                "no-change runs in human_issue_approval state.",
                sub_id,
                consecutive_no_change,
            )
            elapsed = _format_duration(registry.now() - info.created_at)
            summary = (
                f"Ticket has been stuck at human_issue_approval for "
                f"{human_approval_cap} consecutive no-change runs "
                f"({elapsed} elapsed) — auto-escalating."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="human_approval_timeout",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(
                    closed, summary, "human_approval_timeout"
                )
            return None

    idle_cap = env.settings.subsessions.max_idle_runs
    if idle_cap > 0 and consecutive_no_change >= idle_cap:
        logger.info(
            "Subsession %s: auto-pausing after %d consecutive no-change runs.",
            sub_id,
            consecutive_no_change,
        )
        summary = (
            f"Auto-paused after {idle_cap} consecutive no-change runs. "
            f"To resume monitoring, send a message to this subsession "
            f"via message_subsession — the monitor will wake and re-check "
            f"the ticket state on its next run."
        )
        paused = registry.mark_paused(
            sub_id,
            summary=summary,
            reason="paused",
        )
        if paused is not None:
            await env.delivery.deliver_summary(paused, summary, "paused")
            if env.event_sink is not None:
                ticket_id_raw = checkpoint.get("ticket_id")
                ticket_id = ticket_id_raw if isinstance(ticket_id_raw, str) else ""
                last_known = checkpoint.get("last_known_state", "")
                env.event_sink.publish(
                    info.owner_session_id,
                    {
                        "type": SSE_NOTIFICATION_TYPE,
                        "title": f"Monitor auto-paused: {info.title}",
                        "body": (
                            f"Tracked ticket {ticket_id} ({last_known}) — {summary}"
                        ),
                        "urgency": "low",
                        "link": ticket_id,
                    },
                )
            # -- paused wait loop: block on inbox, wake on resume signal --
            return await _paused_wait_loop(
                env, info, sub_id, previous_result, consecutive_no_change
            )
        return None

    no_change_cap = env.settings.subsessions.auto_stop_no_change_runs
    if consecutive_no_change >= no_change_cap:
        logger.warning(
            "Subsession %s: auto-stopping after %d consecutive no-change runs. "
            "The monitor will no longer watch for changes — restart it if "
            "continued monitoring is needed.",
            sub_id,
            consecutive_no_change,
        )
        elapsed = _format_duration(registry.now() - info.created_at)
        state_context = ""
        last_known_state = checkpoint.get("last_known_state", "")
        if isinstance(last_known_state, str) and last_known_state:
            state_context = (
                f" Still '{last_known_state}' after {elapsed} — "
                f"if this is not expected, consider checking step-level "
                f"logs or the ticket timeline."
            )
        else:
            state_context = (
                f" No changes detected over {elapsed}. "
                f"Restart the monitor if continued watching is needed."
            )
        summary = (
            f"Auto-stopped after {no_change_cap} consecutive no-change runs."
            f"{state_context}"
        )
        closed = registry.mark_closed(
            sub_id,
            summary=summary,
            reason="no_change_auto_stop",
            closed_by="system",
        )
        if closed is not None:
            await env.delivery.deliver_summary(closed, summary, "no_change_auto_stop")
        return None

    # Sleep until the next tick, waking early on a steering message.
    woke = await registry.wait_for_inbox(sub_id, timeout=info.interval_seconds)
    pending = registry.drain_inbox(sub_id) if woke else []
    return pending, previous_result, consecutive_no_change


async def _subsession_worker(env: SubsessionEnv, sub_id: str) -> None:
    """Drive one subsession to a terminal state (see module docstring)."""
    registry = env.registry
    info = registry.get(sub_id)
    if info is None:  # pragma: no cover - spawn always registers first
        return
    close_state = CloseState()
    ctx = SubsessionContext(
        owner_session_id=info.owner_session_id,
        subsession_id=sub_id,
        depth=info.depth,
    )
    try:
        # env.agent_factory (-> create_agent_from_settings) calls
        # fetch_roster_sync, which does asyncio.run(...) internally — safe
        # only when no event loop is running. _subsession_worker runs as a
        # task on the server's already-running loop, so calling the factory
        # directly here raises "asyncio.run() cannot be called from a
        # running event loop" for every subsession spawn. Offload to a
        # thread, which has no running loop of its own.
        agent = await asyncio.to_thread(
            env.agent_factory, env.settings, info.model_level, ctx, close_state
        )
        # Seed from any persisted replay window — non-empty when this
        # worker is resuming a periodic subsession after a restart, so
        # the agent picks up with its prior context instead of blank.
        history: list[tuple[str, str]] = list(info.turn_history)
        previous_result: str | None = None
        consecutive_no_change = info.consecutive_no_change
        first_turn = True
        pending: list[InboxMessage] = []

        # -- resume status check for ticket monitors -------------------
        # Extended to all subsession kinds: a TASK or USER_CHAT with a
        # ticket_id in its checkpoint must also verify the ticket is still
        # active before proceeding — a closed ticket means the work is moot.
        if info.checkpoint is not None:
            from .worker_mill import _check_resume_status

            should_continue, context_msg = await _check_resume_status(env, info, sub_id)
            if not should_continue:
                return
            if context_msg is not None:
                pending = [
                    InboxMessage(
                        role="system",
                        text=context_msg,
                        timestamp=env.registry.now(),
                    )
                ]

        # -- paused restart: if the subsession was PAUSED before a server
        #    restart, go straight to the paused wait loop — do not run
        #    an agent turn.
        if info.status is SubsessionStatus.PAUSED:
            logger.info(
                "Subsession %s: restored in PAUSED state — entering wait loop.",
                sub_id,
            )
            result = await _paused_wait_loop(
                env, info, sub_id, previous_result, consecutive_no_change
            )
            if result is None:
                return
            pending, previous_result, consecutive_no_change = result
            # Fall through to the main loop — the subsession is now RUNNING.

        # -- component_request availability check ----------------------
        _cd = getattr(env.settings, "central_deploy", None)
        _cd_url = getattr(_cd, "url", "") if _cd is not None else ""
        if (
            info.kind in (SubsessionKind.PERIODIC, SubsessionKind.WAIT_FOR_EVENT)
            and not _cd_url
        ):
            logger.warning(
                "Periodic subsession %s requires component_request but "
                "central_deploy.url is not configured — the tool is not "
                "available.  Closing subsession to prevent futile retries.",
                sub_id,
            )
            summary = (
                "component_request is not available: "
                "central_deploy.url is not configured. "
                "The monitor cannot fetch ticket state from the board API "
                "and would fail every tick."
            )
            closed = registry.mark_closed(
                sub_id,
                summary=summary,
                reason="missing_tool",
                closed_by="system",
            )
            if closed is not None:
                await env.delivery.deliver_summary(closed, summary, "missing_tool")
            return

        while True:
            # -- verify the subsession is still alive --------------------
            info = registry.get(sub_id)
            if info is None or not info.is_active:
                logger.warning(
                    "Subsession %s is no longer active — worker exiting.", sub_id
                )
                return

            registry.set_status(sub_id, SubsessionStatus.RUNNING)

            # -- WAIT_FOR_EVENT: pre-turn event wait ----------------------
            if (
                info.kind is SubsessionKind.WAIT_FOR_EVENT
                and not first_turn
                and not pending
            ):
                result = await _event_wait_loop(
                    env, info, sub_id, previous_result, consecutive_no_change
                )
                if result is None:
                    return
                pending, previous_result, consecutive_no_change = result

            # -- on_close: wait for the parent session to close, then run
            #    as a one-shot task.  Polls is_session_closed every 5 s;
            #    exits cleanly when the subsession is externally closed
            #    while waiting.
            if info.kind is SubsessionKind.ON_CLOSE and first_turn:
                while not env.conversation_store.is_session_closed(
                    info.owner_session_id
                ):
                    info = registry.get(sub_id)
                    if info is None or not info.is_active:
                        return
                    await asyncio.sleep(5.0)
                # Parent is now closed — proceed as a one-shot task below.

            if info.kind is SubsessionKind.PERIODIC:
                # -- run guard: prevent duplicate execution of run N -----
                next_run = info.runs + 1
                if not registry.claim_run(sub_id, next_run):
                    logger.warning(
                        "Run %d of subsession %s was already executed; "
                        "skipping duplicate.",
                        next_run,
                        sub_id,
                    )
                    # Advance the run counter and retry immediately.  A
                    # collision means the counter is behind completed_runs
                    # (a pre-fix persisted store); sleeping an interval per
                    # historical run number starves the schedule — with
                    # regular restarts the subsession never runs again.
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.RUNNING,
                        runs=next_run,
                    )
                    continue

                steering = pending
                turn_input = _build_periodic_input(
                    info,
                    previous_result,
                    steering,
                    pre_authorized_patterns=env.settings.subsessions.pre_authorized_ticket_patterns,
                )
            elif info.kind is SubsessionKind.WAIT_FOR_EVENT:
                next_run = info.runs + 1
                if not registry.claim_run(sub_id, next_run):
                    logger.warning(
                        "Run %d of subsession %s was already executed; "
                        "skipping duplicate.",
                        next_run,
                        sub_id,
                    )
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.RUNNING,
                        runs=next_run,
                    )
                    continue
                steering = pending
                turn_input = _build_wait_for_event_input(
                    info,
                    previous_result,
                    steering,
                    pre_authorized_patterns=env.settings.subsessions.pre_authorized_ticket_patterns,
                )
            elif first_turn:
                turn_input = info.prompt
                if info.kind is SubsessionKind.USER_CHAT:
                    turn_input = _USER_CHAT_FIRST_TURN_NOTE + "\n\n" + turn_input
            else:
                turn_input = _render_turn_input(pending)
            first_turn = False

            try:
                reply = await _run_turn_with_transient_retry(
                    env,
                    agent,
                    turn_input,
                    history,
                    sub_id,
                    info,
                )
            except _RunTimeoutError:
                # Periodic runs continue the schedule after a timeout;
                # task / user_chat runs fail the whole subsession.
                if info.kind in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                ):
                    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
                        logger.warning(
                            "Wait-for-event subsession %s run %d timed out; "
                            "continuing schedule.",
                            sub_id,
                            info.runs + 1,
                        )
                        registry.append_transcript(
                            sub_id,
                            "system",
                            "Run timed out — the agent turn exceeded the"
                            " per-run timeout.",
                        )
                        runs = info.runs + 1
                        registry.set_status(
                            sub_id,
                            SubsessionStatus.SLEEPING,
                            runs=runs,
                            last_result="TIMEOUT",
                        )
                        if env.event_sink is not None:
                            env.event_sink.publish(
                                info.owner_session_id,
                                subsession_result_frame(
                                    sub_id,
                                    info.kind.value,
                                    info.title,
                                    runs,
                                    "TIMEOUT",
                                    info.parent_id,
                                ),
                            )
                        if not info.include_previous_result:
                            previous_result = None
                        consecutive_no_change += 1
                        info.consecutive_no_change = consecutive_no_change
                        # No sleep — the main loop re-enters the event wait.
                        env.registry.reap_orphans()
                        continue
                    logger.warning(
                        "Periodic subsession %s run %d timed out; continuing schedule.",
                        sub_id,
                        info.runs + 1,
                    )
                    registry.append_transcript(
                        sub_id,
                        "system",
                        "Run timed out — the agent turn exceeded the per-run timeout.",
                    )
                    # Advance the run counter so the schedule moves on.
                    runs = info.runs + 1
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.SLEEPING,
                        runs=runs,
                        next_run_at=registry.now() + (info.interval_seconds or 60.0),
                        last_result="TIMEOUT",
                    )
                    # Deliver a timeout result so the parent isn't left
                    # wondering.
                    if env.event_sink is not None:
                        env.event_sink.publish(
                            info.owner_session_id,
                            subsession_result_frame(
                                sub_id,
                                info.kind.value,
                                info.title,
                                runs,
                                "TIMEOUT",
                                info.parent_id,
                            ),
                        )
                    if not info.include_previous_result:
                        previous_result = None
                    consecutive_no_change += 1
                    info.consecutive_no_change = consecutive_no_change
                    # Sleep until next tick, waking early on steering.
                    woke = await registry.wait_for_inbox(
                        sub_id,
                        timeout=info.interval_seconds or 60.0,
                    )
                    pending = registry.drain_inbox(sub_id) if woke else []
                    env.registry.reap_orphans()
                    continue
                # TASK / USER_CHAT: let the outer handler fail the subsession.
                raise
            except _TransientExhaustedError:
                # Periodic runs whose transient errors could not be cleared
                # skip this cycle and continue the schedule instead of
                # failing permanently.
                if info.kind in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                ):
                    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
                        logger.warning(
                            "Wait-for-event subsession %s run %d: transient errors "
                            "exhausted; skipping this cycle.",
                            sub_id,
                            info.runs + 1,
                        )
                        registry.append_transcript(
                            sub_id,
                            "system",
                            "Run skipped — transient API errors persisted "
                            "across all retry attempts.",
                        )
                        runs = info.runs + 1
                        registry.set_status(
                            sub_id,
                            SubsessionStatus.SLEEPING,
                            runs=runs,
                            last_result="TRANSIENT_ERROR",
                        )
                        if env.event_sink is not None:
                            env.event_sink.publish(
                                info.owner_session_id,
                                subsession_result_frame(
                                    sub_id,
                                    info.kind.value,
                                    info.title,
                                    runs,
                                    "TRANSIENT_ERROR",
                                    info.parent_id,
                                ),
                            )
                        if not info.include_previous_result:
                            previous_result = None
                        consecutive_no_change += 1
                        info.consecutive_no_change = consecutive_no_change
                        # No sleep — the main loop re-enters the event wait.
                        env.registry.reap_orphans()
                        continue
                    logger.warning(
                        "Periodic subsession %s run %d: transient errors "
                        "exhausted; skipping this cycle.",
                        sub_id,
                        info.runs + 1,
                    )
                    registry.append_transcript(
                        sub_id,
                        "system",
                        "Run skipped — transient API errors persisted "
                        "across all retry attempts.",
                    )
                    runs = info.runs + 1
                    registry.set_status(
                        sub_id,
                        SubsessionStatus.SLEEPING,
                        runs=runs,
                        next_run_at=registry.now() + (info.interval_seconds or 60.0),
                        last_result="TRANSIENT_ERROR",
                    )
                    if env.event_sink is not None:
                        env.event_sink.publish(
                            info.owner_session_id,
                            subsession_result_frame(
                                sub_id,
                                info.kind.value,
                                info.title,
                                runs,
                                "TRANSIENT_ERROR",
                                info.parent_id,
                            ),
                        )
                    if not info.include_previous_result:
                        previous_result = None
                    consecutive_no_change += 1
                    woke = await registry.wait_for_inbox(
                        sub_id,
                        timeout=info.interval_seconds or 60.0,
                    )
                    pending = registry.drain_inbox(sub_id) if woke else []
                    env.registry.reap_orphans()
                    continue
                # TASK / USER_CHAT: let the outer handler fail the subsession.
                raise

            history.append((turn_input, reply))
            registry.append_turn_history(sub_id, turn_input, reply)
            # Inbox messages were transcripted at enqueue time; only the
            # assistant side is appended here.
            registry.append_transcript(sub_id, "assistant", reply)

            # -- agent-requested close (any kind) --------------------------
            if close_state.requested:
                summary = close_state.summary or reply
                closed = registry.mark_closed(
                    sub_id, summary=summary, reason="completed", closed_by="agent"
                )
                if closed is not None:
                    await env.delivery.deliver_summary(closed, summary, "completed")
                elif not close_state.delivery_done:
                    # Already closed by the complete_subsession tool (which
                    # persists immediately to survive a process restart).
                    # The tool may have already delivered the summary to
                    # survive a race with an external close; only deliver
                    # here when the tool did NOT already deliver.
                    closed_info = registry.get(sub_id)
                    if closed_info is not None:
                        await env.delivery.deliver_summary(
                            closed_info, summary, "completed"
                        )
                return

            # -- kind-specific continuation --------------------------------
            continuation = await _handle_kind_continuation(
                env, info, sub_id, reply, previous_result, consecutive_no_change
            )
            if continuation is None:
                return
            pending, previous_result, consecutive_no_change = continuation
            info.consecutive_no_change = consecutive_no_change

    except asyncio.CancelledError:
        # External close already set the terminal state and (if wanted)
        # delivered the summary — nothing to do here.
        raise
    except Exception as exc:
        logger.exception("Subsession %s worker failed", sub_id)
        error_msg = _format_worker_error(exc)

        # -- retry for user_chat / task kinds --------------------------
        info = registry.get(sub_id)
        if info is not None and info.kind in (
            SubsessionKind.USER_CHAT,
            SubsessionKind.TASK,
            SubsessionKind.ON_CLOSE,
        ):
            max_retries = env.settings.subsessions.user_chat_max_retries
            if info.retry_count < max_retries:
                info.retry_count += 1
                info._last_error = error_msg
                retry_notice = _RETRY_PROMPT_TEMPLATE.format(
                    attempt=info.retry_count,
                    max_retries=max_retries,
                    error=error_msg,
                )
                # Prepend the retry notice to the original prompt.
                # Strip a prior retry notice if present so they don't
                # accumulate across attempts.
                if _RETRY_PROMPT_TEMPLATE.split("{", 1)[0] in info.prompt:
                    # There is a prior retry notice — replace it.
                    info.prompt = info.prompt.split("]\n\n", 1)[-1]
                info.prompt = retry_notice + info.prompt
                # Persist the retry state before re-launching so a
                # crash restart picks up the updated retry_count.
                registry.persist()
                logger.info(
                    "Subsession %s: retry %d/%d after error: %s",
                    sub_id,
                    info.retry_count,
                    max_retries,
                    error_msg,
                )
                await _subsession_worker(env, sub_id)
                return

        # -- exhausted retries or non-retryable kind ------------------
        failed = registry.fail(sub_id, error=error_msg)
        if failed is not None:
            summary = failed.summary or f"Failed: {error_msg}"
            # For user_chat subsessions that exhausted retries, append
            # the original prompt so the operator can answer the
            # decision directly in the main conversation — the
            # side-chat panel is no longer available.
            if (
                failed.kind is SubsessionKind.USER_CHAT
                and info is not None
                and info.retry_count >= env.settings.subsessions.user_chat_max_retries
            ):
                summary += (
                    "\n\nThe side-chat could not be delivered after "
                    f"{info.retry_count} retries. "
                    "You can answer the original decision here:\n\n"
                    f"{info.prompt}"
                )
            await env.delivery.deliver_summary(failed, summary, "failed")


async def _handle_kind_continuation(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str | None, int] | None:
    """Dispatch kind-specific post-turn logic.

    Returns ``(pending, previous_result, consecutive_no_change)`` to
    continue, or ``None`` to stop (the subsession reached a terminal
    state).
    """
    if info.kind is SubsessionKind.TASK or info.kind is SubsessionKind.ON_CLOSE:
        pending = await _run_task_turn(env, sub_id, reply)
        if not pending:
            return None
        return (pending, None, 0)

    if info.kind is SubsessionKind.USER_CHAT:
        pending = await _run_user_chat_turn(env, sub_id)
        return (pending, None, 0)

    # PERIODIC
    if info.kind is SubsessionKind.PERIODIC:
        result = await _run_periodic_turn(
            env, info, sub_id, reply, previous_result, consecutive_no_change
        )
        if result is None:
            return None
        pending, previous_result, consecutive_no_change = result
        env.registry.reap_orphans()
        return (pending, previous_result, consecutive_no_change)

    # WAIT_FOR_EVENT
    if info.kind is SubsessionKind.WAIT_FOR_EVENT:
        result = await _run_wait_for_event_turn(
            env, info, sub_id, reply, previous_result, consecutive_no_change
        )
        if result is None:
            return None
        pending, previous_result, consecutive_no_change = result
        env.registry.reap_orphans()
        return (pending, previous_result, consecutive_no_change)
