"""Delegation and check-loop tool factories for the foreground chat agent.

Lets the foreground agent offload long-running work to a background sub-agent
via :func:`~robotsix_chat.chat.runner.spawn_subagent_task`. The tool returns
a task id immediately — the foreground reply is never blocked.

Also lets the model launch a recurring check loop via
:func:`~robotsix_chat.chat.loops.spawn_check_loop`.

Usage::

    from robotsix_chat.chat.delegation import (
        build_check_loop_tools,
        build_delegation_tools,
        ConversationDeliveryChannel,
        NullDeliveryChannel,
    )

    tools = build_delegation_tools(
        settings, registry, channel, client_id="browser-1",
    )
    loop_tools = build_check_loop_tools(
        settings, check_loop_registry, client_id="browser-1",
    )
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.chat.loops import (
    CheckLoopRegistry,
    LoopCapacityError,
    LoopIntervalError,
    spawn_check_loop,
)
from robotsix_chat.chat.runner import (
    NULL_CHANNEL,
    DeliveryChannel,
    NullDeliveryChannel,  # noqa: F401  # re-exported for external consumers
    TaskCapacityError,
    spawn_subagent_task,
    task_started_frame,
)
from robotsix_chat.chat.server import ChatAgent
from robotsix_chat.chat.tasks import TaskRegistry
from robotsix_chat.config import Settings

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore

logger = logging.getLogger(__name__)


class ConversationDeliveryChannel:
    """Record completed/failed background-task & loop results into ConversationStore.

    When a delegated task finishes (or fails), the foreground agent needs to
    learn about the outcome so it can relay task ids, URLs, and findings to
    the user on its **next** turn.  This channel bridges that gap by writing a
    synthetic turn into the store keyed by the originating ``client_id``.

    The same mechanism is used for check-loop ticks: each tick's
    ``loop_tick`` frame (and any ``loop_failed`` frame) is recorded as a
    synthetic turn so the foreground agent sees the tick output in its
    next-turn history.

    ``task_started``, ``loop_started``, and ``loop_stopped`` frames are
    intentionally ignored — the user already sees those via the SSE path —
    so the conversation history stays clean.

    Constructor takes the shared :class:`ConversationStore` instance (the same
    object passed to ``run_server`` via ``app.state.conversation_store``).
    """

    def __init__(self, store: ConversationStore) -> None:
        """Create a channel that writes to *store*."""
        self._store = store

    async def publish(self, client_id: str, frame: dict[str, Any]) -> None:
        """Record *frame* into the conversation store for *client_id*.

        *client_id* is the owning browser identity (``owner_id`` in the
        multi-session model).  The frame is recorded into that owner's
        current active session.

        Dispatches on ``frame["type"]``:

        * ``"task_completed"`` — reads ``frame["task_id"]`` and
          ``frame["result"]`` and records a turn so the agent sees the result
          in its next-turn history.
        * ``"task_failed"`` — reads ``frame["task_id"]`` and ``frame["error"]``
          and records a turn conveying the failure.
        * ``"loop_tick"`` — reads ``frame["loop_id"]``,
          ``frame["iteration"]``, and ``frame["result"]`` and records a turn
          so the agent sees the tick output in its next-turn history.
        * ``"loop_failed"`` — reads ``frame["loop_id"]`` and
          ``frame["error"]`` and records a turn conveying the loop failure.
        * ``"task_started"``, ``"loop_started"``, ``"loop_stopped"``, and any
          other/unknown type — no-op.
        * Empty/falsy *client_id* — no-op.

        Best-effort: never raises out to the runner's ``_worker``.
        """
        if not client_id:
            return

        frame_type = frame.get("type")
        task_id = frame.get("task_id", "")

        if frame_type == "task_completed":
            result = frame.get("result", "")
            self._store.record_for_owner(
                client_id,
                f"[Background task {task_id} completed]",
                str(result),
            )
        elif frame_type == "task_failed":
            error = frame.get("error", "")
            self._store.record_for_owner(
                client_id,
                f"[Background task {task_id} failed]",
                f"Error: {str(error)}",
            )
        elif frame_type == "loop_tick":
            loop_id = frame.get("loop_id", "")
            iteration = frame.get("iteration")
            result = frame.get("result", "")
            self._store.record_for_owner(
                client_id,
                f"[Check loop {loop_id} tick {iteration}]",
                str(result),
            )
        elif frame_type == "loop_failed":
            loop_id = frame.get("loop_id", "")
            error = frame.get("error", "")
            self._store.record_for_owner(
                client_id,
                f"[Check loop {loop_id} failed]",
                f"Error: {str(error)}",
            )


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def build_delegation_tools(
    settings: Settings,
    registry: TaskRegistry,
    channel: DeliveryChannel,
    *,
    client_id: str = "",
    agent_factory: Callable[[Settings], ChatAgent] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``delegate_task`` tool for the foreground chat agent.

    When wired into the agent's tools list, the model can call
    ``delegate_task`` to offload long-running work to a background sub-agent
    of the same tier.

    *client_id* is captured lexically in the returned tool closure so it
    survives the claude_sdk / MCP execution-context boundary — unlike a
    ContextVar, which is invisible there.

    *agent_factory* is forwarded to
    :func:`~robotsix_chat.chat.runner.spawn_subagent_task`; when ``None``
    (the default), the runner's own default factory is used — which builds
    a sub-agent via :func:`~robotsix_chat.chat.server.create_agent_from_settings`
    with **no** delegation tools (preventing infinite recursion).
    """

    async def delegate_task(task_description: str) -> str:
        """Delegate long-running work to a background sub-agent.

        Use this when the user asks for work that will take a while —
        multi-step research, long generation, or anything that would stall
        the reply.  The task runs in the background and you get a task id
        immediately.

        Args:
            task_description: A complete, self-contained description of what
                the background sub-agent should do.  Include all necessary
                context — the sub-agent is a fresh instance with no
                conversation history.

        Returns:
            A message with the started task's id; relay it to the user so
            they know the work is in progress and they'll be notified on
            completion.

        """
        cid = client_id

        # Build kwargs, omitting agent_factory when None so the runner uses
        # its own default (create_agent_from_settings, no delegation tools).
        kwargs: dict[str, Any] = dict(
            client_id=cid,
            prompt=task_description,
            settings=settings,
            registry=registry,
            channel=channel,
        )
        if agent_factory is not None:
            kwargs["agent_factory"] = agent_factory

        try:
            task_id = spawn_subagent_task(**kwargs)
        except TaskCapacityError as exc:
            logger.info("delegate_task rejected: %s", exc)
            return (
                "I couldn't start a new background task right now — too many are "
                "already running. Ask me again once some have finished."
            )

        # Best-effort: publish a task_started frame so any listening SSE
        # channel learns about the task immediately.  When the channel
        # raises, we log and swallow — the task is already registered.
        try:
            await channel.publish(cid, task_started_frame(task_id, task_description))
        except Exception:
            logger.exception("Failed to publish task_started for %s", task_id)

        return (
            f"Started background task {task_id}. "
            "It is running now; I'll let you know when it finishes."
        )

    return [delegate_task]


# ---------------------------------------------------------------------------
# Check-loop tool factory
# ---------------------------------------------------------------------------


def _no_change_result(result: str) -> bool:
    """Return ``True`` when *result* indicates no noteworthy change.

    Matches results that are empty, whitespace-only, or start with the
    ``NO_CHANGE`` sentinel marker.  Used as the default ``suppress_when``
    predicate so the user is not spammed with no-op tick notifications.
    """
    stripped = result.strip()
    return not stripped or stripped.upper().startswith("NO_CHANGE")


def build_check_loop_tools(
    settings: Settings,
    registry: CheckLoopRegistry,
    channel: DeliveryChannel = NULL_CHANNEL,
    *,
    client_id: str = "",
    agent_factory: Callable[[Settings], ChatAgent] | None = None,
) -> list[Callable[..., Any]]:
    """Return the check-loop tools for the foreground chat agent.

    When wired into the agent's tools list, the model can call
    ``start_check_loop`` to launch a recurring check on the user's behalf.

    *channel* is captured lexically and forwarded to
    :func:`~robotsix_chat.chat.loops.spawn_check_loop` so each tick's
    result is written back into the originating conversation via
    :class:`ConversationDeliveryChannel`.

    *client_id* is captured lexically in the returned tool closure so it
    survives the claude_sdk / MCP execution-context boundary — unlike a
    ContextVar, which is invisible there.

    *agent_factory* is forwarded to
    :func:`~robotsix_chat.chat.loops.spawn_check_loop`; when ``None``
    (the default), the runner's own default factory is used — which builds
    a sub-agent with **no** loop tools (preventing infinite recursion).

    Ticks whose result matches the ``NO_CHANGE`` sentinel (or are empty)
    are suppressed — no SSE frame is published and no conversation turn is
    recorded — so the user is only notified when something actually
    changed.  The loop still tracks iterations and the last result
    internally so the next tick can compare against prior state.
    """

    async def start_check_loop(
        check_description: str,
        interval_seconds: float,
        max_iterations: int | None = None,
        reason: str | None = None,
        include_previous_result: bool = False,
    ) -> str:
        """Start a recurring background check that re-runs every ``interval_seconds``.

        Use this when the user asks you to watch something over time — monitor a
        price, poll an endpoint, check for new results, etc.  The check runs in
        a fresh sub-agent with no conversation history, so *check_description*
        must be complete and self-contained (include all context the sub-agent
        needs).

        The check re-runs automatically until it is explicitly stopped, reaches
        *max_iterations* (if set), or self-stops.  Every result is surfaced to
        the user as it lands.

        When *include_previous_result* is ``True``, each iteration after the
        first receives the previous tick's result prepended to the prompt so the
        sub-agent can compare against prior state.  Use this for
        change-detection checks: instruct the sub-agent to return a description
        when something changed, or the exact text ``NO_CHANGE`` when nothing
        changed (ticks marked ``NO_CHANGE`` are suppressed — no user
        notification).

        Args:
            check_description: A complete, self-contained description of what
                the background check sub-agent should do on every iteration.
                The sub-agent is a fresh instance with no conversation history —
                include all necessary context.
            interval_seconds: How often to re-run the check, in seconds.  The
                minimum is 60 seconds; shorter intervals are rejected.
            max_iterations: Optional cap on the number of iterations.  When the
                loop reaches this many ticks it stops automatically.  ``None``
                (the default) means it runs until explicitly stopped.
            reason: A short human-readable summary of what this loop checks
                for (e.g. "Monitor stock price" or "Watch for new emails").
                Displayed in the UI to help the user identify the loop at a
                glance.  When omitted, the UI falls back to a truncated
                prompt.
            include_previous_result: When ``True``, each tick after the first
                receives the previous tick's result so the sub-agent can
                compare state across iterations.  Default ``False``.

        Returns:
            A message with the started loop's id; relay it to the user so they
            know the check is running and how often it will re-run.

        """
        cid = client_id
        try:
            loop_id = spawn_check_loop(
                client_id=cid,
                prompt=check_description,
                interval_seconds=interval_seconds,
                settings=settings,
                registry=registry,
                max_iterations=max_iterations,
                suppress_when=_no_change_result,
                include_previous_result=include_previous_result,
                agent_factory=agent_factory,
                channel=channel,
                reason=reason,
            )
        except LoopIntervalError as exc:
            logger.info("start_check_loop rejected (interval): %s", exc)
            return (
                f"I can't start a check loop with an interval of "
                f"{interval_seconds:g} seconds — the minimum is 60 seconds. "
                f"Please ask again with a longer interval."
            )
        except LoopCapacityError as exc:
            logger.info("start_check_loop rejected (capacity): %s", exc)
            return (
                "I couldn't start a new check loop right now — too many are "
                "already running. Ask me again once some have finished."
            )
        return (
            f"Started check loop {loop_id}. It will re-run every "
            f"{interval_seconds:g}s; I'll surface each result as it lands."
        )

    async def stop_check_loop(loop_id: str) -> str:
        """Stop a running check loop by its id.

        Use this when the user asks you to cancel, stop, or end a recurring
        check.  The *loop_id* is the identifier returned by
        ``start_check_loop``; you can also discover active loop ids via
        ``list_check_loops``.

        Only loops owned by this conversation can be stopped — you cannot
        interfere with another client's checks.  Stopping an already-stopped
        (or nonexistent) loop is harmless and returns a polite message.

        To **change a loop's interval or prompt**, stop the running loop with
        this tool, then start a new one with ``start_check_loop`` — there is
        no in-place edit for interval or prompt.

        Args:
            loop_id: The id of the loop to stop (from ``start_check_loop``'s
                return value or from ``list_check_loops``).

        Returns:
            A confirmation message on success, or a polite notice when the
            loop is not found / not owned by this client.

        """
        info = registry.get(loop_id)
        if info is None or info.client_id != client_id:
            logger.info(
                "stop_check_loop: loop %s not found for client %s", loop_id, client_id
            )
            return (
                f"I don't see a check loop with id '{loop_id}' that belongs "
                f"to this conversation.  Use ``list_check_loops`` to see your "
                f"active loops and their ids."
            )
        registry.stop(loop_id, reason="stopped by assistant")
        logger.info("stop_check_loop: stopped loop %s", loop_id)
        return f"Stopped check loop {loop_id}."

    async def list_check_loops() -> str:
        """List all active check loops owned by this conversation.

        Use this to discover what recurring checks are currently running so
        you can report status to the user or obtain an *loop_id* to pass to
        ``stop_check_loop``.  Loops belonging to other conversations are
        never shown.

        Returns:
            A human-readable summary of this client's loops — one line per
            loop with id, status, interval, iteration count, and a prompt
            snippet — or a message that there are none.

        """
        loops = registry.list_for_client(client_id)
        if not loops:
            return "There are no check loops running in this conversation."
        lines: list[str] = []
        for info in loops:
            snippet = info.prompt[:80].replace("\n", " ")
            lines.append(
                f"- {info.id}: {info.status.value}, "
                f"every {info.interval_seconds:g}s, "
                f"iteration {info.iterations}"
                f"{f' of {info.max_iterations}' if info.max_iterations else ''}"
                f' — "{snippet}…"'
            )
        return "Active check loops:\n" + "\n".join(lines)

    return [start_check_loop, stop_check_loop, list_check_loops]
