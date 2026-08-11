"""Agent tool factory for the subsession system.

``build_subsession_tools`` returns the tool callables an agent gets,
depending on where it sits in the tree:

* **Spawn/steer/close/list tools** — for the main chat agent (depth 0)
  and any subsession agent whose children would still be within the
  configured ``max_depth``.
* **``complete_subsession``** — only for subsession agents (a
  :class:`~robotsix_chat.subsessions.worker.CloseState` is supplied);
  lets the agent end its own subsession with a summary.

All identity (owner session, own subsession id, depth) is captured
**lexically** in the closures — tool calls cross the claude_sdk/MCP
boundary where ambient context does not survive.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .models import (
    ACTIVE_STATUSES,
    SubsessionCapacityError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionNoChangeThresholdError,
    SubsessionPeriodicSpawnError,
    SubsessionUserChatSpawnError,
    SubsessionWaitForEventSpawnError,
)
from .registry import SubsessionRegistry
from .worker import CloseState, SubsessionContext, SubsessionEnv, spawn_subsession

logger = logging.getLogger(__name__)

_KIND_VALUES = ", ".join(k.value for k in SubsessionKind)


def build_subsession_tools(
    env: SubsessionEnv,
    *,
    ctx: SubsessionContext,
    close_state: CloseState | None = None,
) -> list[Any]:
    """Return the subsession tools for an agent at *ctx*'s tree position.

    *close_state* is the worker-shared close holder — pass it for
    subsession agents (enables ``complete_subsession``); the main chat
    agent passes ``None`` and gets no self-close tool.
    """
    tools: list[Any] = []
    cfg = env.settings.subsessions

    if ctx.depth < cfg.max_depth:
        tools.extend(_build_spawn_and_control_tools(env, ctx))
    if close_state is not None and ctx.subsession_id is not None:
        tools.append(
            _build_complete_tool(
                close_state, ctx.subsession_id, env.registry, env.delivery
            )
        )
        tools.append(_build_set_checkpoint_tool(ctx.subsession_id, env.registry))
        tools.append(
            _build_self_update_tool(
                ctx.subsession_id, env.registry, env.settings.subsessions
            )
        )
        tools.append(
            _build_spawn_continuation_tool(env, ctx.subsession_id, close_state)
        )
    return tools


def _scope_ids(env: SubsessionEnv, ctx: SubsessionContext) -> set[str]:
    """Ids of subsessions *ctx*'s agent may steer/close/list.

    The main agent (depth 0) controls the owning session's whole tree; a
    subsession agent controls only its own descendants.
    """
    if ctx.subsession_id is None:
        return {info.id for info in env.registry.list_for_owner(ctx.owner_session_id)}
    return {info.id for info in env.registry.list_descendants(ctx.subsession_id)}


def _resolve_subsession_id(
    env: SubsessionEnv, ctx: SubsessionContext, candidate: str
) -> str | None:
    """Resolve *candidate* to a full subsession id in scope.

    Tries exact match first, then prefix match (so the agent can pass
    the 8-char truncated id that ``list_subsessions`` displays).  Returns
    ``None`` when there is no match or the prefix is ambiguous.
    """
    scope = _scope_ids(env, ctx)
    if candidate in scope:
        return candidate
    matches = [sid for sid in scope if sid.startswith(candidate)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning(
            "Ambiguous subsession prefix %r matches %d ids: %s",
            candidate,
            len(matches),
            ", ".join(matches),
        )
    return None


def _build_spawn_and_control_tools(
    env: SubsessionEnv, ctx: SubsessionContext
) -> list[Any]:
    """Build spawn/message/close/list closures bound to *ctx*."""
    default_level = env.settings.subsessions.default_model_level

    async def spawn_subsession_tool(
        kind: str,
        title: str,
        instructions: str,
        model_level: int | None = None,
        interval_seconds: float | None = None,
        max_runs: int | None = None,
        auto_stop_no_change_runs: int | None = None,
        include_previous_result: bool = False,
        inherit_context: bool = False,
        dedup_key: str | None = None,
    ) -> str:
        """Start a background subsession and return its id immediately.

        kind is one of: "task" (one-shot background job — runs the
        instructions to completion and reports a summary back to this
        conversation), "periodic" (re-runs the instructions every
        interval_seconds until closed — for monitoring/polling; the
        sub-agent replies NO_CHANGE when nothing changed and calls
        complete_subsession when the watched condition is terminal),
        "wait_for_event" (event-driven ticket monitor — waits for
        mill push events instead of polling; wakes only when a matching
        ticket state-change event arrives or a safety-net timeout fires;
        no interval_seconds needed; the sub-agent verifies state via the
        board API on every wake and calls complete_subsession when the
        ticket reaches a terminal state),
        "user_chat" (opens a side-chat with the user for a focused
        question or discussion — use it instead of blocking this
        conversation on a pending decision; the user replies in a
        dedicated panel and a summary comes back here when it closes),
        or "on_close" (like a one-shot task, but the subsession waits
        until this parent session is closed before executing — use it
        to schedule work that should fire when the conversation ends,
        such as proposing the next autonomous job).

        instructions must be complete and self-contained — the subsession
        agent starts with NO conversation history. For user_chat decision
        subsessions, every option presented to the operator (Option A,
        Option B, …) MUST include a one-line self-contained definition in
        the instructions, e.g. "Option A: deploy immediately with no grace
        period. Option B: phased rollout with a 7-day warning gate." The
        subsession agent is instructed to restate these definitions inline
        on every operator-facing turn — but it can only do that if you
        provide them. title is a short human-readable label shown in the
        UI panel. Set inherit_context
        to True to automatically prepend an ancestor context block (the
        root task and each ancestor's title/prompt summary) so a nested
        child does not start from scratch — useful when spawning from a
        subsession that itself runs a focused sub-task. model_level picks
        capability 1 (cheapest) to 4 (frontier, most expensive) — match
        it to difficulty: 1 for trivial polling/extraction, 2 for
        general work (the default choice unless the task needs stronger
        reasoning), 3 for reasoning 2 struggles with, 4 only for
        genuinely hard reasoning. Levels 1-2 need an OpenRouter API key.
        If a spawn errors with an API key message, retry at level 3
        (keyless) and tell the user the key could not be found by the
        server's config file — do NOT claim the key is missing outright:
        you cannot inspect the environment or secrets to confirm, and the
        key may be set in a location the server does not read.
        Recommend the operator verify the `llmio.api_key` field in the
        server's JSON config file.
        interval_seconds (minimum applies), max_runs, and
        auto_stop_no_change_runs are for kind="periodic" only.
        auto_stop_no_change_runs overrides the global
        subsessions.auto_stop_no_change_runs auto-stop threshold for
        this monitor only (must be an integer >= 1). For a long-lived
        ticket monitor that naturally progresses over days (e.g. waiting
        on human review or CI), pass a higher value such as 50 so it does
        not auto-stop after the default 3 consecutive NO_CHANGE runs.

        dedup_key is for kind="user_chat", kind="periodic", and
        kind="wait_for_event": a short,
        stable string that identifies a known issue or ticket (e.g. the
        exact error message prefix, like "asyncio.run() cannot be called",
        or a ticket id like "5f1c"). When set and a subsession with the
        same dedup_key is already active, no new subsession is created —
        the existing id is returned instead. Use this to prevent duplicate
        side-chats for a single root cause (e.g. a process-wide
        asyncio.run crash affecting many ticket monitors) and duplicate
        periodic monitors for the same ticket. For monitors, pass the
        ticket id returned by the filing endpoint as dedup_key. Always
        check list_subsessions first to see what is already running.

        The subsession runs in the background; you will receive its
        summary in this conversation when it closes. Use
        message_subsession to steer it while it runs.
        """
        try:
            kind_enum = SubsessionKind(kind)
        except ValueError:
            return f"Unknown kind {kind!r} — expected one of: {_KIND_VALUES}."
        if env.conversation_store.is_session_closed(ctx.owner_session_id):
            return "This session is closed — no new subsessions can be started."

        # Reject wait_for_event spawns without a dedup_key synchronously —
        # the ticket_id is required for event filtering and cannot be
        # reliably extracted from the instructions alone.
        if kind_enum is SubsessionKind.WAIT_FOR_EVENT and not dedup_key:
            return (
                "wait_for_event subsessions require a dedup_key "
                "(the ticket id) — pass the ticket id returned by the "
                "filing endpoint as dedup_key.  Without it the monitor "
                "cannot filter incoming events and will fail to start."
            )

        # Periodic/monitor sibling spawning and forbidden-kind pre-check.
        # When the spawning agent is a periodic or wait_for_event subsession:
        #   - ALLOWED: task (remediation) and user_chat (escalation) spawn
        #     as SIBLINGS attached to the holding parent conversation, not
        #     nested under the monitor itself.
        #   - FORBIDDEN: periodic, wait_for_event (nested monitors —
        #     runaway risk) and on_close (no meaningful use case from a
        #     monitor).
        #   - Forbidden attempts are rejected silently (logged, no
        #     operator-facing escalation).
        effective_parent_id = ctx.subsession_id
        effective_depth = ctx.depth + 1
        if ctx.subsession_id is not None:
            agent_info = env.registry.get(ctx.subsession_id)
            if agent_info is not None and agent_info.kind in (
                SubsessionKind.PERIODIC,
                SubsessionKind.WAIT_FOR_EVENT,
            ):
                if kind_enum in (
                    SubsessionKind.PERIODIC,
                    SubsessionKind.WAIT_FOR_EVENT,
                    SubsessionKind.ON_CLOSE,
                ):
                    logger.warning(
                        "%s subsession %s attempted forbidden spawn "
                        "kind=%s — rejected silently.",
                        agent_info.kind.value,
                        ctx.subsession_id,
                        kind,
                    )
                    return (
                        f"Monitors cannot spawn {kind} subsessions. "
                        "Use 'task' for remediation or 'user_chat' for "
                        "escalation instead."
                    )
                # Sibling spawn: attach to the periodic's parent at the
                # periodic's own depth (peer, not child).
                effective_parent_id = agent_info.parent_id
                effective_depth = agent_info.depth

        # Check whether a dedup hit is expected before calling
        # spawn_subsession — after a fresh create the new record
        # always matches its own dedup_key, so a post-hoc check cannot
        # tell whether the spawn was fresh or deduplicated.
        was_dedup = False
        if dedup_key is not None:
            was_dedup = env.registry.is_dedup_key_active(dedup_key) is not None
            if not was_dedup and kind_enum in (
                SubsessionKind.PERIODIC,
                SubsessionKind.WAIT_FOR_EVENT,
            ):
                was_dedup = (
                    env.registry.find_active_periodic_by_ticket_id(dedup_key)
                    is not None
                )

        # Pre-populate the checkpoint with ticket_id for ticket monitors
        # (periodic and event-driven) — the dedup_key is always the
        # ticket id for monitors, so writing it into the checkpoint at
        # spawn time activates the CI-verification guard from the first
        # run and prevents the monitor from closing before it verifies
        # CI workflow results.
        checkpoint: dict[str, object] | None = None
        if dedup_key is not None and kind_enum in (
            SubsessionKind.PERIODIC,
            SubsessionKind.WAIT_FOR_EVENT,
        ):
            # Verify the ticket exists on the board before spawning the
            # monitor — a monitor for a stale/paraphrased ticket ID would
            # waste agent turns before auto-pausing, then the watcher
            # would poll 404s indefinitely.
            direct_repo = getattr(env.settings, "direct_repo", None)
            if direct_repo is not None and getattr(direct_repo, "enabled", False):
                board_url = getattr(direct_repo, "board_api_base_url", "")
                if board_url:
                    try:
                        ticket_url = f"{board_url.rstrip('/')}/tickets/{dedup_key}"
                        async with httpx.AsyncClient(
                            timeout=httpx.Timeout(10.0)
                        ) as client:
                            response = await client.get(ticket_url)
                            if response.status_code == 404:
                                logger.warning(
                                    "spawn_subsession_tool: ticket %r not "
                                    "found (404) — refusing to spawn "
                                    "monitor.",
                                    dedup_key,
                                )
                                return (
                                    f"Cannot spawn monitor for ticket "
                                    f"'{dedup_key}': ticket not found "
                                    f"(404) on the board — the ID may be "
                                    f"paraphrased or stale.  Verify the "
                                    f"ticket ID against the board ticket "
                                    f"list before retrying."
                                )
                            # Non-2xx non-404: log but allow the spawn —
                            # the board may be temporarily unhealthy and
                            # we don't want to block all monitor spawns.
                            if response.status_code >= 400:
                                logger.warning(
                                    "spawn_subsession_tool: board returned "
                                    "%d for ticket %r — allowing spawn "
                                    "but ticket may be unreachable.",
                                    response.status_code,
                                    dedup_key,
                                )
                    except httpx.TimeoutException, httpx.ConnectError, OSError:
                        # Board unreachable — allow the spawn to proceed;
                        # the watcher will catch repeated 404s later.
                        logger.warning(
                            "spawn_subsession_tool: board unreachable "
                            "when verifying ticket %r — allowing spawn.",
                            dedup_key,
                        )
                    except Exception:
                        logger.exception(
                            "spawn_subsession_tool: unexpected error "
                            "verifying ticket %r — allowing spawn.",
                            dedup_key,
                        )
            checkpoint = {"ticket_id": dedup_key}

        try:
            sub_id = spawn_subsession(
                env=env,
                kind=kind_enum,
                owner_session_id=ctx.owner_session_id,
                parent_id=effective_parent_id,
                depth=effective_depth,
                title=title,
                prompt=instructions,
                model_level=model_level if model_level is not None else default_level,
                interval_seconds=interval_seconds,
                include_previous_result=include_previous_result,
                max_runs=max_runs,
                auto_stop_no_change_runs=auto_stop_no_change_runs,
                inherit_context=inherit_context,
                dedup_key=dedup_key,
                checkpoint=checkpoint,
                event_timeout_seconds=(
                    env.settings.subsessions.event_driven_timeout_seconds
                    if kind_enum is SubsessionKind.WAIT_FOR_EVENT
                    else None
                ),
            )
        except SubsessionCapacityError as exc:
            return (
                f"Could not start the subsession: {exc}\n"
                "The subsession pool is full, so no new monitor can start "
                "right now. To work around this, either:\n"
                "  1. call list_subsessions and close or pause idle/resolved "
                "monitors (especially ones reporting no change for many "
                "cycles) to free a slot, then retry;\n"
                "  2. poll the ticket/subject manually in this conversation "
                "instead of starting a monitor;\n"
                "  3. retry later once an active monitor closes on its own.\n"
                "Do NOT retry in a tight loop — the pool will not free itself."
            )
        except (
            SubsessionDepthError,
            SubsessionIntervalError,
            SubsessionLevelError,
            SubsessionNoChangeThresholdError,
            SubsessionPeriodicSpawnError,
            SubsessionWaitForEventSpawnError,
            SubsessionUserChatSpawnError,
        ) as exc:
            return f"Could not start the subsession: {exc}"
        if was_dedup:
            return (
                f"Deduplicated: an active subsession for key "
                f"'{dedup_key}' already exists ({sub_id}) — returning "
                f"the existing id instead of spawning a duplicate."
            )
        return f"Started {kind} subsession {sub_id} ('{title}')."

    spawn_subsession_tool.__name__ = "spawn_subsession"
    spawn_subsession_tool.__qualname__ = "spawn_subsession"

    async def message_subsession(subsession_id: str, text: str) -> str:
        """Send a steering message to one of your running subsessions.

        The subsession sees the message at its next turn boundary (after
        its current step finishes) — use this to refine instructions,
        add context, or redirect work without restarting it. Only
        subsessions started from this conversation (or their
        descendants) can be messaged.
        """
        resolved = _resolve_subsession_id(env, ctx, subsession_id)
        if resolved is None:
            return f"No subsession {subsession_id!r} in this conversation's tree."
        if env.registry.enqueue_message(resolved, "parent", text):
            return (
                f"Message queued for subsession {subsession_id} — it will "
                "be seen when its current step finishes."
            )
        return f"Subsession {subsession_id} is no longer active."

    async def close_subsession(subsession_id: str, reason: str | None = None) -> str:
        """Close one of your running subsessions from the outside.

        The subsession's worker is cancelled and a best-effort summary
        (its last reported state) is still delivered back to this
        conversation. Prefer letting a subsession finish on its own —
        use this when its work is no longer needed or it is stuck.
        """
        resolved = _resolve_subsession_id(env, ctx, subsession_id)
        if resolved is None:
            return f"No subsession {subsession_id!r} in this conversation's tree."
        closed = env.registry.cancel_and_close(
            resolved,
            reason=reason or "closed by parent",
            closed_by="parent",
        )
        if closed is None:
            return f"Subsession {subsession_id} is already closed."
        await env.delivery.deliver_summary(
            closed, closed.summary or "", closed.close_reason or "closed"
        )
        return f"Closed subsession {subsession_id}. Summary: {closed.summary}"

    async def list_subsessions() -> str:
        """List this conversation's subsessions (yours and their children).

        Returns one line per subsession: id, kind, status, model level,
        title, and scheduling info for periodic ones. Use it to check on
        running work before spawning duplicates.
        """
        if ctx.subsession_id is None:
            infos = env.registry.list_for_owner(ctx.owner_session_id)
        else:
            infos = env.registry.list_descendants(ctx.subsession_id)
        if not infos:
            return "No subsessions in this conversation."
        return "\n".join(_format_info(info) for info in infos)

    return [
        spawn_subsession_tool,
        message_subsession,
        close_subsession,
        list_subsessions,
    ]


def _build_complete_tool(
    close_state: CloseState,
    sub_id: str,
    registry: SubsessionRegistry,
    delivery: Any,
) -> Any:
    """Build the self-close tool bound to *close_state*."""

    async def complete_subsession(summary: str) -> str:
        """Close THIS subsession and report summary to whoever started it.

        Call it when your work is finished (task), when the discussion
        with the user has reached a conclusion (user_chat), or when the
        monitored condition has reached a terminal state (periodic — do
        not keep re-reporting a finished state). summary must be a
        concise, self-contained account of the outcome — it is the only
        thing your parent conversation is guaranteed to see. The
        subsession ends after your current reply.

        The summary will be shown directly to a human operator — write
        in plain, user-facing language.  Omit internal technical details:
        block IDs, event numbers, state machine transitions, spawn
        counters, internal timeout values, stack traces, or raw API
        response fragments.  State the actionable conclusion first (what
        happened in one sentence), then any supporting detail the
        operator needs to act.  For example, instead of "event 35
        triggered stall guard after spawn counter reset at block a3f2,"
        write "Publishing workflow stalled because the ticket scope was
        too broad — suggest splitting into a canary-only ticket."

        The close is persisted to disk immediately so the subsession is
        not re-loaded after a restart — always call this BEFORE any
        action that might kill the process (e.g. a self-restart).
        """
        info = registry.get(sub_id)
        if info is None or not info.is_active:
            return (
                f"Error: subsession {sub_id} is no longer active — its tree "
                "record may have been lost. Cannot complete."
            )

        # -- minimum-runs guard: prevent one-and-done monitors ---------
        # Periodic ticket monitors must complete at least one run before
        # they are allowed to self-close.  Without this guard a monitor
        # that detects a transient milestone (e.g. a PR merge) on its
        # very first tick calls complete_subsession and exits before it
        # ever verifies CI results — the user then finds no monitor
        # running and misses downstream failures.
        if (
            info.kind == SubsessionKind.PERIODIC
            and info.checkpoint
            and "ticket_id" in info.checkpoint
            and info.runs < 1
        ):
            return (
                "REJECTED: minimum runs not met.  This periodic monitor "
                "has not completed any runs yet — it must observe at "
                "least one full tick before calling complete_subsession, "
                "to ensure the monitored condition is genuinely terminal "
                "and not a transient milestone.  Continue monitoring: "
                "reply NO_CHANGE if the ticket state has not changed "
                "since the last run, or report the current state if it "
                "has changed.  Call complete_subsession on a subsequent "
                "run once the terminal condition is confirmed."
            )

        # -- loop guard: require CI workflow verification for ticket monitors --
        if (
            info.kind == SubsessionKind.PERIODIC
            and info.checkpoint
            and "ticket_id" in info.checkpoint
        ):
            summary_lower = summary.lower()
            has_ci_evidence = (
                "ci workflow" in summary_lower
                or "workflow run" in summary_lower
                or "pipeline" in summary_lower
                or "github actions" in summary_lower
                or "publish" in summary_lower
                or "deploy workflow" in summary_lower
                or "could not be verified" in summary_lower
            )
            if not has_ci_evidence:
                return (
                    "REJECTED: CI workflow verification required.  This periodic "
                    "monitor is watching a ticket (checkpoint.ticket_id is set).  "
                    "Before calling complete_subsession you MUST verify the most "
                    "recent CI workflow run for the affected pipeline (e.g. the "
                    "'Publish Docker image' workflow or the repo's primary deploy "
                    "workflow).  Use the check_workflow_run tool (or the GitHub "
                    "Actions API via component_request) to fetch the latest run "
                    "status.  Your summary MUST include the workflow verification "
                    "result — either 'CI workflow passed', 'CI workflow failed: "
                    "<details>', or 'CI workflow status could not be verified'.  "
                    "Then call complete_subsession again with the updated summary."
                )

        # Persist the closed state immediately so the subsession is not
        # re-loaded on restart.  The return value is intentionally ignored:
        # the pre-check guarantees success (info is active), and the worker's
        # post-turn check calls mark_closed again (idempotent — returns None
        # for an already-closed subsession) and handles delivery.
        registry.mark_closed(
            sub_id, summary=summary, reason="completed", closed_by="agent"
        )
        # Deliver the summary immediately from within the tool so that
        # even if an external close (HTTP endpoint) cancels the worker
        # before it can deliver, the parent conversation still receives
        # the outcome.  The worker skips its own delivery when this flag
        # is set, avoiding a duplicate reaction turn.
        close_state.requested = True
        close_state.summary = summary
        close_state.delivery_done = True
        # Fire-and-forget: errors are logged inside deliver_summary,
        # never surfaced to the agent.
        await delivery.deliver_summary(info, summary, "completed")
        return (
            "Close requested — this subsession will end after the current "
            "reply and the summary will be delivered."
        )

    return complete_subsession


def _build_set_checkpoint_tool(sub_id: str, registry: SubsessionRegistry) -> Any:
    """Build the checkpoint-update tool bound to *sub_id*."""

    async def set_checkpoint(data: dict[str, object]) -> str:
        """Update this subsession's checkpoint with arbitrary key/value data.

        The checkpoint persists across restarts — use it to store state
        that recovery needs: monitored ticket id, last-known ticket state,
        completion criteria, consecutive-failure counters, etc.  All keys
        must be strings; values can be strings, numbers, bools, lists, or
        nested dicts.  Pass an empty dict to clear the checkpoint.

        Only the most recent call's data is kept — each call REPLACES the
        entire checkpoint, so include ALL the fields you want to keep.
        Exceptions (system-owned keys are preserved automatically even
        when omitted): for ``wait_for_event`` monitors the ``ticket_id``
        key, and for ``periodic`` monitors the ``auto_stop_no_change_runs``
        override.  All other keys are replaced wholesale.
        """
        if not isinstance(data, dict):
            return "set_checkpoint: data must be a dict of string keys."
        cleaned: dict[str, object] = {}
        for k, v in data.items():
            if not isinstance(k, str):
                return f"set_checkpoint: key {k!r} is not a string."
            cleaned[str(k)] = v
        ok = registry.update_checkpoint(sub_id, cleaned or None)
        if not ok:
            return "set_checkpoint: this subsession is no longer active."
        return f"Checkpoint updated ({len(cleaned)} keys)."

    return set_checkpoint


def _build_spawn_continuation_tool(
    env: SubsessionEnv,
    sub_id: str,
    close_state: CloseState,
) -> Any:
    """Build the ``spawn_continuation`` tool bound to *sub_id*.

    Returns a callable that lets a subsession agent re-launch itself as a
    fresh TASK subsession carrying a continuation cursor, then close the
    current subsession.  This is the escape hatch for long-running batch
    operations (ticket triage, remediation sweeps) that would otherwise
    exhaust their run budget — the agent checkpoints its progress,
    spawns a sibling continuation, and exits.
    """

    async def spawn_continuation(
        summary: str,
        resume_instructions: str,
    ) -> str:
        """Close this subsession and spawn a sibling continuation.

        Use this when you have processed a batch of work but cannot
        finish the full backlog within your run budget — it checkpoints
        your progress and re-launches the task so the next subsession
        picks up where you left off.

        summary is a concise, self-contained account of what was
        accomplished so far (delivered to the parent conversation).
        resume_instructions tells the continuation agent exactly where
        to resume — be specific: "continue listing tickets from
        ticket ID X", "resume triage at page 3", etc.  The continuation
        receives your original instructions PLUS this context.

        After this call the current subsession closes immediately and
        its worker stops.  The new subsession starts as a sibling (same
        parent, same tree depth) with the same title and model level.
        Your checkpoint data (set via set_checkpoint) is carried forward
        automatically.
        """
        registry = env.registry
        info = registry.get(sub_id)
        if info is None or not info.is_active:
            return (
                "spawn_continuation: this subsession is no longer active — "
                "cannot spawn a continuation."
            )

        # -- build continuation prompt --------------------------------
        # Preserve the original prompt (stripped of any retry-notice
        # prefix that the worker loop may have prepended) so the
        # continuation starts from the real instructions, not a
        # retry-error preamble.
        original_prompt: str = info.prompt
        retry_marker = "[RETRY "
        if retry_marker in original_prompt:
            original_prompt = original_prompt.split("]\n\n", 1)[-1]

        continuation_prompt = (
            f"{original_prompt}\n\n"
            f"=== CONTINUATION CONTEXT ===\n"
            f"This subsession is a continuation of a prior run "
            f"({sub_id}) that processed a batch but did not finish.  "
            f"The prior run's summary:\n{summary}\n\n"
            f"Resume instructions:\n{resume_instructions}\n\n"
            f"IMPORTANT: Do NOT re-process work that was already "
            f"completed.  Start from the point described in the "
            f"resume instructions above."
        )

        # -- close current subsession BEFORE spawning the continuation,
        #    so any dedup_key the current subsession holds does not
        #    collide with the new spawn.
        registry.mark_closed(
            sub_id, summary=summary, reason="continued", closed_by="agent"
        )
        close_state.requested = True
        close_state.summary = summary
        close_state.delivery_done = True
        # Fire-and-forget delivery (errors are logged, never surfaced).
        await env.delivery.deliver_summary(info, summary, "continued")

        # -- spawn the continuation as a sibling (same parent + depth)
        try:
            new_id = spawn_subsession(
                env=env,
                kind=SubsessionKind.TASK,
                owner_session_id=info.owner_session_id,
                parent_id=info.parent_id,
                depth=info.depth,
                title=info.title,
                prompt=continuation_prompt,
                model_level=info.model_level,
                checkpoint=info.checkpoint,
            )
        except (
            SubsessionCapacityError,
            SubsessionDepthError,
            SubsessionLevelError,
        ) as exc:
            return (
                f"spawn_continuation: this subsession is closed but the "
                f"continuation could not be spawned: {exc}\n"
                f"The parent conversation will receive the summary — "
                f"the operator can manually re-launch from the checkpoint."
            )

        return (
            f"Continuation spawned as subsession {new_id} — "
            f"this subsession ({sub_id}) is now closed.  "
            f"The new subsession will pick up from the resume point above."
        )

    return spawn_continuation


def _build_self_update_tool(
    sub_id: str,
    registry: SubsessionRegistry,
    cfg: Any,
) -> Any:
    """Build the self-update tool for periodic subsessions."""
    min_interval = cfg.min_interval_seconds

    async def self_update_subsession(
        instructions: str | None = None,
        interval_seconds: float | None = None,
        max_runs: int | None = None,
    ) -> str:
        info = registry.get(sub_id)
        if info is None or not info.is_active:
            return f"self_update_subsession: subsession {sub_id} is not active."
        if info.kind is not SubsessionKind.PERIODIC:
            return (
                "self_update_subsession: only periodic subsessions can "
                "self-update — this subsession is kind "
                f"'{info.kind.value}'."
            )

        changed: list[str] = []

        if instructions is not None:
            if not isinstance(instructions, str):
                return "self_update_subsession: instructions must be a string."
            if len(instructions) > 8000:
                return (
                    "self_update_subsession: instructions too long "
                    f"({len(instructions)} chars, max 8000)."
                )
            changed.append("instructions")

        if interval_seconds is not None:
            if not isinstance(interval_seconds, (int, float)):
                return "self_update_subsession: interval_seconds must be a number."
            if interval_seconds < min_interval:
                return (
                    "self_update_subsession: interval_seconds must be "
                    f">= {min_interval}s (got {interval_seconds})."
                )
            changed.append("interval")

        if max_runs is not None:
            if not isinstance(max_runs, int):
                return "self_update_subsession: max_runs must be an integer."
            if max_runs < 0:
                return "self_update_subsession: max_runs must be >= 0."
            changed.append("max_runs")

        if not changed:
            return (
                "self_update_subsession: no fields to update — pass at "
                "least one of instructions, interval_seconds, or max_runs."
            )

        ok = registry.update_periodic_config(
            sub_id,
            prompt=instructions if instructions is not None else None,
            interval_seconds=interval_seconds,
            max_runs=max_runs,
        )
        if not ok:
            return (
                "self_update_subsession: update failed — subsession may "
                "have closed between the guard check and the write."
            )

        fields = ", ".join(changed)
        return f"Self-update applied: changed {fields}.  Effective next tick."

    self_update_subsession.__doc__ = (
        "Update THIS periodic subsession's own run configuration.\n"  # nosec B608
        "\n"
        "Call this to change what a periodic monitor does or how often it\n"
        "runs — the natural alternative to spawning a new periodic child\n"
        "(which is not allowed from within a periodic context).  Changes\n"
        "take effect on the next scheduled tick.\n"
        "\n"
        "instructions: rewrite or extend the instruction text this\n"
        "  subsession executes each tick — e.g. add a second ticket id to\n"
        "  watch, change the terminal-state criteria.  Must not exceed\n"
        "  8000 characters.  Omit (or pass None) to leave unchanged.\n"
        "interval_seconds: change the polling interval (minimum "
        f"{min_interval}s applies).  Omit (or pass None) to leave "
        "unchanged.\n"
        "max_runs: adjust the remaining max-run cap.  Pass None to remove\n"
        "  the cap entirely.  The run counter is NEVER reset — self-update\n"
        "  cannot bypass max-run limits.\n"
        "\n"
        "Only works from within a periodic subsession.  Returns a\n"
        "confirmation string listing which fields were changed.\n"
    )

    return self_update_subsession


def _format_info(info: SubsessionInfo) -> str:
    """Render one ``list_subsessions`` line for *info*."""
    indent = "  " * max(0, info.depth - 1)
    parts = [
        f"{indent}{info.id[:8]}",
        f"[{info.kind.value}]",
        info.status.value,
        f"L{info.model_level}",
        f"'{info.title}'",
    ]
    if info.kind is SubsessionKind.PERIODIC and info.interval_seconds:
        parts.append(f"every {info.interval_seconds:.0f}s, {info.runs} runs")
        if info.status in ACTIVE_STATUSES and info.next_run_at:
            wait = max(0.0, info.next_run_at - time.time())
            parts.append(f"next in {wait:.0f}s")
    age = time.time() - info.last_activity_at
    parts.append(f"active {age:.0f}s ago")
    return " ".join(parts)
