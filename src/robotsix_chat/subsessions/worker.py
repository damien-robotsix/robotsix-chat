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
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from robotsix_chat.chat.events import subsession_result_frame

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

# Reply sentinel a periodic subsession uses to report "nothing changed".
_NO_CHANGE_SENTINEL = "NO_CHANGE"

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
    if kind is SubsessionKind.PERIODIC and parent_id is not None:
        parent = env.registry.get(parent_id)
        if parent is not None and parent.kind is SubsessionKind.PERIODIC:
            raise SubsessionPeriodicSpawnError(
                "periodic subsessions cannot spawn periodic children"
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
        )
    except SubsessionDedupError as exc:
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
        f"Reply with the single word {_NO_CHANGE_SENTINEL} — and nothing "
        "else, no punctuation, no commentary — only if genuinely nothing "
        "changed since the previous run: the observed state is identical "
        "to the prior run. If any state transition occurred (e.g. draft → "
        "implement_complete, in_progress → done, ready → in_progress), "
        "DO NOT reply NO_CHANGE. Instead, acknowledge the change with a "
        "concise line summarising what changed and, when appropriate, "
        'offer a next step (e.g. "Ticket 5f1c has moved to '
        "implement_complete; PR #654 is open. Let me know if you'd like "
        'me to check on the review status."). '
        "Reserve multi-paragraph "
        "reports for substantive changes: first-time blocking, completion, "
        "failure, or transitions requiring user action.\n\n"
        "Decision-blocked tickets: when the monitored ticket is awaiting an "
        "operator decision — stuck in human_issue_approval, waiting on an "
        '"Option A or B?" choice, or otherwise blocked on a human '
        "direction — do NOT silently reply NO_CHANGE run after run.  "
        "Instead, report the blocked state with a concise note that "
        'recommends pausing: "Ticket is awaiting operator decision. '
        "Consider pausing this monitor until the operator provides "
        'direction."  This surfaces the pause recommendation so the '
        "operator can act on it rather than waiting for the auto-stop "
        "timeout.\n\n"
        "When a ticket reaches a terminal state (done/closed), your "
        "complete_subsession summary MUST include a note about whether a "
        "PR was merged for this ticket — check the ticket events/history "
        "for merge events (look for 'merged', 'auto-merged', 'merge commit', "
        "or similar). Do NOT report 'no PR URL' as a concern without first "
        "checking whether a PR was actually merged; a ticket can be closed "
        "via an auto-merged PR even when the pr_url field is absent or null. "
        "If a PR was merged, say so; if no PR was involved at all, say "
        '"closed without a PR" instead of "no PR URL". '
        "Call complete_subsession when the monitored condition reaches a "
        "terminal state."
    )
    return "\n\n".join(parts)


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


async def _run_periodic_turn(
    env: SubsessionEnv,
    info: SubsessionInfo,
    sub_id: str,
    reply: str,
    previous_result: str | None,
    consecutive_no_change: int,
) -> tuple[list[InboxMessage], str, int] | None:
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
    if not suppressed:
        if env.event_sink is not None:
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
        await env.delivery.deliver_result(info, runs, reply)
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
    checkpoint = info.checkpoint or {}
    last_known = checkpoint.get("last_known_state", "")
    if isinstance(last_known, str) and last_known.lower() == "human_issue_approval":
        human_approval_cap = env.settings.subsessions.human_approval_timeout_runs
        if consecutive_no_change >= human_approval_cap:
            logger.warning(
                "Subsession %s: auto-escalating after %d consecutive "
                "no-change runs in human_issue_approval state.",
                sub_id,
                consecutive_no_change,
            )
            summary = (
                f"Ticket has been stuck at human_issue_approval for "
                f"{human_approval_cap} consecutive no-change runs — "
                f"auto-escalating."
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

    no_change_cap = env.settings.subsessions.auto_stop_no_change_runs
    if consecutive_no_change >= no_change_cap:
        logger.warning(
            "Subsession %s: auto-stopping after %d consecutive no-change runs. "
            "The monitor will no longer watch for changes — restart it if "
            "continued monitoring is needed.",
            sub_id,
            consecutive_no_change,
        )
        summary = f"Auto-stopped after {no_change_cap} consecutive no-change runs."
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
        consecutive_no_change = 0
        first_turn = True
        pending: list[InboxMessage] = []

        # -- resume status check for ticket monitors -------------------
        if info.kind is SubsessionKind.PERIODIC and info.checkpoint is not None:
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

        while True:
            # -- verify the subsession is still alive --------------------
            info = registry.get(sub_id)
            if info is None or not info.is_active:
                logger.warning(
                    "Subsession %s is no longer active — worker exiting.", sub_id
                )
                return

            registry.set_status(sub_id, SubsessionStatus.RUNNING)
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
                turn_input = _build_periodic_input(info, previous_result, steering)
            elif first_turn:
                turn_input = info.prompt
                if info.kind is SubsessionKind.USER_CHAT:
                    turn_input = _USER_CHAT_FIRST_TURN_NOTE + "\n\n" + turn_input
            else:
                turn_input = _render_turn_input(pending)
            first_turn = False

            try:
                reply = await _run_turn_with_timeout(
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
                if info.kind is SubsessionKind.PERIODIC:
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

    except asyncio.CancelledError:
        # External close already set the terminal state and (if wanted)
        # delivered the summary — nothing to do here.
        raise
    except Exception as exc:
        logger.exception("Subsession %s worker failed", sub_id)
        failed = registry.fail(sub_id, error=str(exc))
        if failed is not None:
            await env.delivery.deliver_summary(
                failed, failed.summary or f"Failed: {exc}", "failed"
            )


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
    if info.kind is SubsessionKind.TASK:
        pending = await _run_task_turn(env, sub_id, reply)
        if not pending:
            return None
        return (pending, None, 0)

    if info.kind is SubsessionKind.USER_CHAT:
        pending = await _run_user_chat_turn(env, sub_id)
        return (pending, None, 0)

    # PERIODIC
    result = await _run_periodic_turn(
        env, info, sub_id, reply, previous_result, consecutive_no_change
    )
    if result is None:
        return None
    pending, previous_result, consecutive_no_change = result
    env.registry.reap_orphans()
    return (pending, previous_result, consecutive_no_change)
