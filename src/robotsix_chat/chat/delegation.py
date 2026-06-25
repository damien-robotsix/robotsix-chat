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
        settings, registry, channel, session_id="session-1",
    )
    loop_tools = build_check_loop_tools(
        settings, check_loop_registry, session_id="session-1",
    )
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robotsix_chat.chat.events import loop_reply_frame
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
    from robotsix_chat.chat.events import EventBus

logger = logging.getLogger(__name__)


class ConversationDeliveryChannel:
    """Record background-task & loop results, and trigger tick-driven agent runs.

    When a delegated task finishes (or fails), the foreground agent needs to
    learn about the outcome so it can relay task ids, URLs, and findings to
    the user on its **next** turn.  This channel bridges that gap by writing a
    synthetic turn into the **exact session** that spawned the work
    (``session_id``).

    For **non-suppressed check-loop ticks**, when the optional dependencies
    (``event_bus``, ``run_serializer``, ``agent_factory``, ``settings``) are
    wired, the channel goes further: it schedules a serialized foreground
    agent run, records the tick result as user input plus the agent's answer
    as a single turn, and emits the answer to the browser via the EventBus
    as a ``loop_reply`` SSE frame.  When they are not wired, ``loop_tick``
    falls back to the legacy behaviour of recording a passive synthetic
    assistant turn (no agent run).

    ``task_started``, ``loop_started``, and ``loop_stopped`` frames are
    intentionally ignored — the user already sees those via the SSE path —
    so the conversation history stays clean.

    Session scoping: background tasks and check loops are tied to the
    ``session_id`` that spawned them.  Both the recorded turn and the
    ``loop_tick`` / ``loop_reply`` SSE frames target that exact session — so a
    result always lands in the conversation it belongs to, even if the user has
    since switched to a different session in the browser.  The run is
    serialized against concurrent ``/chat`` requests for the **same session**.
    """

    def __init__(
        self,
        store: ConversationStore,
        *,
        event_bus: EventBus | None = None,
        run_serializer: object | None = None,  # RunSerializer (avoid circular import)
        agent_factory: Callable[[Settings], ChatAgent] | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Create a channel wired to *store* and optionally to *event_bus*.

        When *event_bus*, *run_serializer*, *agent_factory*, and *settings*
        are all provided, ``loop_tick`` frames trigger a serialized foreground
        agent run (see class docstring).  When any of them is ``None``,
        ``loop_tick`` falls back to the legacy behaviour of recording a
        passive synthetic assistant turn (no agent run).
        """
        self._store = store
        self._event_bus = event_bus
        self._run_serializer = run_serializer
        self._agent_factory = agent_factory
        self._settings = settings
        # Strong references to in-flight tick-triggered runs so they are
        # not GC'd before completion (mirrors spawn_subagent_task /
        # spawn_check_loop keeping task refs).
        self._pending_runs: set[asyncio.Task[None]] = set()
        # True when all optional deps are present — enables the tick-run path.
        self._tick_runs_enabled = (
            event_bus is not None
            and run_serializer is not None
            and agent_factory is not None
            and settings is not None
        )

    async def publish(self, session_id: str, frame: dict[str, Any]) -> None:
        """Record *frame* into the conversation store for *session_id*.

        *session_id* is the chat session that spawned the background work.
        The frame is recorded into that exact session (not an owner's active
        session), so results land in the conversation they belong to.

        Dispatches on ``frame["type"]``:

        * ``"task_completed"`` — records a turn with the task result.
        * ``"task_failed"`` — records a turn conveying the failure.
        * ``"loop_tick"`` — when tick runs are enabled, schedules a
          serialized foreground agent run (records one ``(tick_input,
          agent_answer)`` turn, emits ``loop_reply`` SSE).  Otherwise falls
          back to recording a passive synthetic assistant turn.
        * ``"loop_failed"`` — records a turn conveying the loop failure.
        * Other types / empty *session_id* — no-op.

        Best-effort: never raises out to the runner's ``_worker``.
        """
        if not session_id:
            return

        frame_type = frame.get("type")
        task_id = frame.get("task_id", "")

        if frame_type == "task_completed":
            result = frame.get("result", "")
            self._store.record_for_session(
                session_id,
                f"[Background task {task_id} completed]",
                str(result),
            )
        elif frame_type == "task_failed":
            error = frame.get("error", "")
            self._store.record_for_session(
                session_id,
                f"[Background task {task_id} failed]",
                f"Error: {str(error)}",
            )
        elif frame_type == "loop_tick":
            loop_id = frame.get("loop_id", "")
            iteration = frame.get("iteration")
            result = frame.get("result", "")
            if self._tick_runs_enabled:
                # Schedule a background task — best-effort, never raises.
                task = asyncio.create_task(
                    self._run_tick_agent(
                        session_id=session_id,
                        loop_id=str(loop_id),
                        iteration=int(iteration) if iteration is not None else 0,
                        result=str(result),
                    )
                )
                self._pending_runs.add(task)
                task.add_done_callback(self._pending_runs.discard)
            else:
                # Legacy path: record a passive synthetic assistant turn.
                self._store.record_for_session(
                    session_id,
                    f"[Check loop {loop_id} tick {iteration}]",
                    str(result),
                )
        elif frame_type == "loop_failed":
            loop_id = frame.get("loop_id", "")
            error = frame.get("error", "")
            self._store.record_for_session(
                session_id,
                f"[Check loop {loop_id} failed]",
                f"Error: {str(error)}",
            )

    # ------------------------------------------------------------------
    # Tick-triggered foreground agent run
    # ------------------------------------------------------------------

    async def _run_tick_agent(
        self,
        *,
        session_id: str,
        loop_id: str,
        iteration: int,
        result: str,
    ) -> None:
        """Run the foreground agent on a tick result, record, and emit reply.

        Must never raise — called as a background task from :meth:`publish`.
        """
        try:
            # Acquire the per-session serializer so this run does not overlap
            # with a concurrent /chat request or another tick for the same
            # session.
            async with self._run_serializer.for_owner(session_id):  # type: ignore[union-attr]
                await self._run_tick_agent_locked(
                    session_id=session_id,
                    loop_id=loop_id,
                    iteration=iteration,
                    result=result,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Tick-triggered agent run failed for loop %s", loop_id)

    async def _run_tick_agent_locked(
        self,
        *,
        session_id: str,
        loop_id: str,
        iteration: int,
        result: str,
    ) -> None:
        """Core of the tick-triggered run — the lock is already held."""
        # Read history for the exact session that owns the loop (read-only;
        # does not touch last_activity or persist).
        history = self._store.history(session_id)

        # The tick result becomes the user-side input of the run turn.
        tick_input = f"[Check loop {loop_id} tick {iteration}]\n\n{result}"

        # Invoke the foreground agent (built WITHOUT check-loop tools).
        if self._agent_factory is None or self._settings is None:
            return  # should never happen — guarded by caller
        agent = self._agent_factory(self._settings)
        reply = "".join(
            [
                chunk
                async for chunk in agent.stream(
                    tick_input,
                    history=history,
                    session_id=session_id,
                    client_id=session_id,
                )
            ]
        )

        # Record exactly one (tick_input, reply) turn into that session.
        self._store.record_for_session(session_id, tick_input, reply)

        # Emit the reply to the browser via the EventBus.
        if self._event_bus is not None:
            self._event_bus.publish(
                session_id,
                loop_reply_frame(loop_id, iteration, reply),
            )


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def build_delegation_tools(
    settings: Settings,
    registry: TaskRegistry,
    channel: DeliveryChannel,
    *,
    session_id: str = "",
    agent_factory: Callable[[Settings], ChatAgent] | None = None,
    conversation_store: Any = None,
) -> list[Callable[..., Any]]:
    """Return the ``delegate_task`` tool for the foreground chat agent.

    When wired into the agent's tools list, the model can call
    ``delegate_task`` to offload long-running work to a background sub-agent
    of the same tier.

    *session_id* is captured lexically in the returned tool closure so it
    survives the claude_sdk / MCP execution-context boundary — unlike a
    ContextVar, which is invisible there.  The spawned task is scoped to this
    session.

    *agent_factory* is forwarded to
    :func:`~robotsix_chat.chat.runner.spawn_subagent_task`; when ``None``
    (the default), the runner's own default factory is used — which builds
    a sub-agent via :func:`~robotsix_chat.chat.server.create_agent_from_settings`
    with **no** delegation tools (preventing infinite recursion).

    *conversation_store* is an optional :class:`ConversationStore` used to
    gate task spawning: when the session is marked ``closed`` the tool
    refuses to spawn new work and returns an explanatory message.
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
        if _is_board_action(task_description):
            logger.warning(
                "delegate_task blocked: board action routed to consult_mill "
                "(task_description preview: %.80s)",
                task_description,
            )
            return (
                "I can't delegate board/ticket work to a background task — "
                "those results are never returned. Use the consult_mill tool "
                "to perform this board action inline instead."
            )

        # Gate: refuse to spawn work in a closed session.
        if conversation_store is not None:
            try:
                if conversation_store.is_session_closed(session_id):
                    logger.info(
                        "delegate_task blocked: session %s is closed", session_id
                    )
                    return (
                        "I can't start a new background task — this session "
                        "has been closed. No new work can be spawned."
                    )
            except Exception:
                # If the store check itself fails (e.g. the store was torn
                # down), let the task attempt proceed — the registry will
                # still enforce capacity.
                logger.debug(
                    "delegate_task: session-closed check failed for session %s",
                    session_id,
                    exc_info=True,
                )

        sid = session_id

        # Build kwargs, omitting agent_factory when None so the runner uses
        # its own default (create_agent_from_settings, no delegation tools).
        kwargs: dict[str, Any] = dict(
            session_id=sid,
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
            await channel.publish(sid, task_started_frame(task_id, task_description))
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


# ---------------------------------------------------------------------------
# Board-action detector — hard-blocks delegate_task from offloading
# board/mill work to a background sub-agent (whose result is never
# returned to the foreground).  Such work must be done inline via
# consult_mill.
# ---------------------------------------------------------------------------


_BOARD_ACTION_PATTERNS = re.compile(
    r"\b("
    r"tickets?|board|mill|triage|epics?|backlog|"
    r"consult_mill|file\s+(?:a|an)\b|log\s+(?:a|an)?\s*bug|"
    r"report\s+(?:a|an)?\s*bug|open\s+(?:a|an)?\s*(?:ticket|issue)|"
    r"create\s+(?:a|an)?\s*(?:ticket|issue)"
    r")\b",
    re.IGNORECASE,
)


def _is_board_action(task_description: str) -> bool:
    """Return ``True`` when *task_description* describes a board/mill action.

    Used to hard-block ``delegate_task`` from offloading board work to a
    background sub-agent (whose result is never returned to the foreground).
    Such work must be done inline via ``consult_mill``.
    """
    return bool(_BOARD_ACTION_PATTERNS.search(task_description))


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
    session_id: str = "",
    agent_factory: Callable[[Settings], ChatAgent] | None = None,
    conversation_store: Any = None,
) -> list[Callable[..., Any]]:
    """Return the check-loop tools for the foreground chat agent.

    When wired into the agent's tools list, the model can call
    ``start_check_loop`` to launch a recurring check on the user's behalf.

    *channel* is captured lexically and forwarded to
    :func:`~robotsix_chat.chat.loops.spawn_check_loop` so each tick's
    result is written back into the originating conversation via
    :class:`ConversationDeliveryChannel`.

    *session_id* is captured lexically in the returned tool closure so it
    survives the claude_sdk / MCP execution-context boundary — unlike a
    ContextVar, which is invisible there.  Loops started, stopped, and listed
    by these tools are scoped to this session.

    *agent_factory* is forwarded to
    :func:`~robotsix_chat.chat.loops.spawn_check_loop`; when ``None``
    (the default), the runner's own default factory is used — which builds
    a sub-agent with **no** loop tools (preventing infinite recursion).

    *conversation_store* is an optional :class:`ConversationStore` used to
    gate loop spawning: when the session is marked ``closed`` the tool
    refuses to spawn new work and returns an explanatory message.

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
        verify_via_board: bool = False,
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
            verify_via_board: Set ``True`` when this check reports mill/board/
                thread/ticket status; the loop will then be required to read the
                board (consult_mill) each tick before reporting, and unverified
                status is suppressed.  Leave ``False`` (the default) for checks
                that never read the board (e.g. price/endpoint polling).

        Returns:
            A message with the started loop's id; relay it to the user so they
            know the check is running and how often it will re-run.

        """
        sid = session_id

        # Gate: refuse to spawn work in a closed session.
        if conversation_store is not None:
            try:
                if conversation_store.is_session_closed(sid):
                    logger.info("start_check_loop blocked: session %s is closed", sid)
                    return (
                        "I can't start a new check loop — this session "
                        "has been closed. No new work can be spawned."
                    )
            except Exception:
                logger.debug(
                    "start_check_loop: session-closed check failed for session %s",
                    sid,
                    exc_info=True,
                )

        try:
            loop_id = spawn_check_loop(
                session_id=sid,
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
                verify_via_board=verify_via_board,
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
        if info is None or info.session_id != session_id:
            logger.info(
                "stop_check_loop: loop %s not found for session %s",
                loop_id,
                session_id,
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
        loops = registry.list_for_session(session_id)
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
