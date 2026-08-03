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

from .models import (
    ACTIVE_STATUSES,
    SubsessionCapacityError,
    SubsessionDepthError,
    SubsessionInfo,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionLevelError,
    SubsessionPeriodicSpawnError,
    SubsessionUserChatSpawnError,
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
        # Periodic self-adjustment tools — only for periodic subsessions.
        info = env.registry.get(ctx.subsession_id)
        if info is not None and info.kind is SubsessionKind.PERIODIC:
            tools.extend(
                _build_periodic_self_adjustment_tools(
                    env, ctx.subsession_id, env.settings
                )
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
        genuinely hard reasoning. Levels 1-2 need an OpenRouter API key;
        if a spawn errors for a missing key, retry at level 3.
        interval_seconds (minimum applies) and max_runs are
        for kind="periodic" only.

        dedup_key is for kind="user_chat" and kind="periodic": a short,
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

        # Periodic/monitor sibling spawning and forbidden-kind pre-check.
        # When the spawning agent is a periodic subsession:
        #   - ALLOWED: task (remediation) and user_chat (escalation) spawn
        #     as SIBLINGS attached to the holding parent conversation, not
        #     nested under the periodic itself.
        #   - FORBIDDEN: periodic (nested monitors — runaway risk) and
        #     on_close (no meaningful use case from a periodic).
        #   - Forbidden attempts are rejected silently (logged, no
        #     operator-facing escalation).
        effective_parent_id = ctx.subsession_id
        effective_depth = ctx.depth + 1
        if ctx.subsession_id is not None:
            agent_info = env.registry.get(ctx.subsession_id)
            if agent_info is not None and agent_info.kind is SubsessionKind.PERIODIC:
                if kind_enum in (SubsessionKind.PERIODIC, SubsessionKind.ON_CLOSE):
                    logger.warning(
                        "Periodic subsession %s attempted forbidden spawn "
                        "kind=%s — rejected silently.",
                        ctx.subsession_id,
                        kind,
                    )
                    return (
                        f"Periodic monitors cannot spawn {kind} subsessions. "
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
            if not was_dedup and kind_enum is SubsessionKind.PERIODIC:
                was_dedup = (
                    env.registry.find_active_periodic_by_ticket_id(dedup_key)
                    is not None
                )

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
                inherit_context=inherit_context,
                dedup_key=dedup_key,
            )
        except (
            SubsessionCapacityError,
            SubsessionDepthError,
            SubsessionIntervalError,
            SubsessionLevelError,
            SubsessionPeriodicSpawnError,
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


def _build_periodic_self_adjustment_tools(
    env: SubsessionEnv,
    sub_id: str,
    settings: Any,
) -> list[Any]:
    """Build self-adjustment tools for a periodic subsession.

    These tools let a periodic monitor revise its own purpose as the
    monitored situation evolves — within operator-configured bounds.
    All mutations are logged at WARNING level for auditability.
    """
    registry = env.registry
    cfg = settings.subsessions
    min_interval = cfg.min_interval_seconds
    max_interval = getattr(cfg, "periodic_max_interval_seconds", 3600.0)
    max_total_runs = getattr(cfg, "periodic_max_total_runs", 100)

    async def update_periodic_instructions(
        new_instructions: str, reason: str | None = None
    ) -> str:
        """Revise this periodic monitor's instructions/prompt.

        Use this to narrow or broaden the monitor's focus as the
        monitored situation evolves (e.g. switch from "watch for any
        change" to "watch for CI failure X once the ticket enters a
        build stage").  Pass the COMPLETE new instructions — they
        replace the old ones entirely.

        The new instructions apply from the NEXT tick onward; the
        current tick (if mid-turn) is unaffected.

        Pass an optional *reason* (one sentence) so the operator can
        see why the monitor changed its behaviour in the audit log.
        """
        if not isinstance(new_instructions, str) or not new_instructions.strip():
            return (
                "update_periodic_instructions: instructions must be a non-empty string."
            )
        info_before = registry.get(sub_id)
        old_len = len(info_before.prompt) if info_before else 0
        ok = registry.update_prompt(sub_id, new_instructions)
        if not ok:
            return "update_periodic_instructions: this subsession is no longer active."
        reason_suffix = f" — {reason}" if reason else ""
        logger.warning(
            "Periodic subsession %s self-adjusted instructions (length %d → %d)%s.",
            sub_id,
            old_len,
            len(new_instructions),
            reason_suffix,
        )
        return "Instructions updated — the new prompt takes effect on the next tick."

    async def adjust_periodic_interval(
        interval_seconds: float, reason: str | None = None
    ) -> str:
        """Adjust this periodic monitor's polling interval (seconds).

        Must be between the configured minimum (default 60 s) and
        maximum (default 3600 s = 1 hour).  Values outside this range
        are clamped to the nearest bound; the clamped value is logged.
        Use shorter intervals when nearing a terminal transition, and
        longer intervals while the monitored subject is idle.

        Pass an optional *reason* (one sentence) so the operator can
        see why the monitor changed its behaviour in the audit log.
        """
        if not isinstance(interval_seconds, (int, float)) or interval_seconds <= 0:
            return (
                "adjust_periodic_interval: interval_seconds must be a positive number."
            )
        original = float(interval_seconds)
        clamped = max(min_interval, min(original, max_interval))
        info_before = registry.get(sub_id)
        old_interval = info_before.interval_seconds if info_before else None
        ok = registry.update_interval(sub_id, clamped)
        if not ok:
            return "adjust_periodic_interval: this subsession is no longer active."
        reason_suffix = f" — {reason}" if reason else ""
        if clamped != original:
            logger.warning(
                "Periodic subsession %s self-adjusted interval "
                "%.1f → %.1f (requested %.1f, clamped to bounds "
                "[%.1f, %.1f])%s.",
                sub_id,
                old_interval if old_interval is not None else clamped,
                clamped,
                original,
                max_interval,
                min_interval,
                reason_suffix,
            )
            return (
                f"Interval adjusted to {clamped:.0f} s "
                f"(requested {original:.0f} s was outside bounds "
                f"[{min_interval:.0f}, {max_interval:.0f}])."
            )
        logger.warning(
            "Periodic subsession %s self-adjusted interval %.1f → %.1f s%s.",
            sub_id,
            old_interval if old_interval is not None else clamped,
            clamped,
            reason_suffix,
        )
        return f"Interval adjusted to {clamped:.0f} s."

    async def adjust_periodic_budget(max_runs: int, reason: str | None = None) -> str:
        """Adjust this periodic monitor's remaining run budget (max_runs).

        Must be between 0 and the configured maximum (default 100).
        Values outside this range are clamped.  Set to 0 to let the
        monitor run until auto-stopped by consecutive NO_CHANGE runs
        or an explicit close.  Use this to extend the budget when a
        ticket needs more monitoring cycles, or shorten it when the
        watched condition is nearing resolution.

        Pass an optional *reason* (one sentence) so the operator can
        see why the monitor changed its behaviour in the audit log.
        """
        if not isinstance(max_runs, int) or max_runs < 0:
            return "adjust_periodic_budget: max_runs must be a non-negative integer."
        clamped = min(max_runs, max_total_runs)
        info_before = registry.get(sub_id)
        old_max_runs = info_before.max_runs if info_before else None
        ok = registry.update_max_runs(sub_id, clamped)
        if not ok:
            return "adjust_periodic_budget: this subsession is no longer active."
        reason_suffix = f" — {reason}" if reason else ""
        if clamped != max_runs:
            logger.warning(
                "Periodic subsession %s self-adjusted budget "
                "%s → %d (requested %d, clamped to max %d)%s.",
                sub_id,
                str(old_max_runs) if old_max_runs is not None else "?",
                clamped,
                max_runs,
                max_total_runs,
                reason_suffix,
            )
            return (
                f"Budget adjusted to {clamped} runs "
                f"(requested {max_runs} exceeds maximum {max_total_runs})."
            )
        logger.warning(
            "Periodic subsession %s self-adjusted budget %s → %d runs%s.",
            sub_id,
            str(old_max_runs) if old_max_runs is not None else "?",
            clamped,
            reason_suffix,
        )
        if clamped == 0:
            return "Budget adjusted to unlimited (runs until auto-stopped or closed)."
        return f"Budget adjusted to {clamped} runs."

    update_periodic_instructions.__name__ = "update_periodic_instructions"
    update_periodic_instructions.__qualname__ = "update_periodic_instructions"
    adjust_periodic_interval.__name__ = "adjust_periodic_interval"
    adjust_periodic_interval.__qualname__ = "adjust_periodic_interval"
    adjust_periodic_budget.__name__ = "adjust_periodic_budget"
    adjust_periodic_budget.__qualname__ = "adjust_periodic_budget"

    return [
        update_periodic_instructions,
        adjust_periodic_interval,
        adjust_periodic_budget,
    ]


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
