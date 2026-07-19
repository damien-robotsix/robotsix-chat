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
        tools.append(_build_complete_tool(close_state, ctx.subsession_id, env.registry))
        tools.append(_build_set_checkpoint_tool(ctx.subsession_id, env.registry))
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
    ) -> str:
        """Start a background subsession and return its id immediately.

        kind is one of: "task" (one-shot background job — runs the
        instructions to completion and reports a summary back to this
        conversation), "periodic" (re-runs the instructions every
        interval_seconds until closed — for monitoring/polling; the
        sub-agent replies NO_CHANGE when nothing changed and calls
        complete_subsession when the watched condition is terminal), or
        "user_chat" (opens a side-chat with the user for a focused
        question or discussion — use it instead of blocking this
        conversation on a pending decision; the user replies in a
        dedicated panel and a summary comes back here when it closes).

        instructions must be complete and self-contained — the subsession
        agent starts with NO conversation history. title is a short
        human-readable label shown in the UI panel. Set inherit_context
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
        try:
            sub_id = spawn_subsession(
                env=env,
                kind=kind_enum,
                owner_session_id=ctx.owner_session_id,
                parent_id=ctx.subsession_id,
                depth=ctx.depth + 1,
                title=title,
                prompt=instructions,
                model_level=model_level if model_level is not None else default_level,
                interval_seconds=interval_seconds,
                include_previous_result=include_previous_result,
                max_runs=max_runs,
                inherit_context=inherit_context,
            )
        except (
            SubsessionCapacityError,
            SubsessionDepthError,
            SubsessionIntervalError,
            SubsessionLevelError,
            SubsessionPeriodicSpawnError,
        ) as exc:
            return f"Could not start the subsession: {exc}"
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
    close_state: CloseState, sub_id: str, registry: SubsessionRegistry
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
        # Persist the closed state immediately so the subsession is not
        # re-loaded on restart.  The worker's post-turn check will see
        # close_state.requested and call mark_closed again (idempotent —
        # returns None for an already-closed subsession).
        registry.mark_closed(
            sub_id, summary=summary, reason="completed", closed_by="agent"
        )
        close_state.requested = True
        close_state.summary = summary
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
