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
from typing import Any

from robotsix_chat.chat.loops import (
    CheckLoopRegistry,
    LoopCapacityError,
    LoopIntervalError,
    spawn_check_loop,
)
from robotsix_chat.chat.runner import (
    DeliveryChannel,
    TaskCapacityError,
    spawn_subagent_task,
    task_started_frame,
)
from robotsix_chat.chat.server import ChatAgent
from robotsix_chat.chat.tasks import TaskRegistry
from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Placeholder delivery channel — a no-op until Ticket 1 lands a concrete
# adapter that bridges to the SSE EventBus.
# ---------------------------------------------------------------------------


class NullDeliveryChannel:
    """A :class:`DeliveryChannel` that drops frames (placeholder)."""

    async def publish(self, client_id: str, frame: dict[str, Any]) -> None:
        """No-op — frames are silently dropped (debug-logged)."""
        logger.debug(
            "NullDeliveryChannel: dropping %r for client %s",
            frame.get("type"),
            client_id,
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


def build_check_loop_tools(
    settings: Settings,
    registry: CheckLoopRegistry,
    *,
    client_id: str = "",
    agent_factory: Callable[[Settings], ChatAgent] | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``start_check_loop`` tool for the foreground chat agent.

    When wired into the agent's tools list, the model can call
    ``start_check_loop`` to launch a recurring check on the user's behalf.

    *client_id* is captured lexically in the returned tool closure so it
    survives the claude_sdk / MCP execution-context boundary — unlike a
    ContextVar, which is invisible there.

    *agent_factory* is forwarded to
    :func:`~robotsix_chat.chat.loops.spawn_check_loop`; when ``None``
    (the default), the runner's own default factory is used — which builds
    a sub-agent with **no** loop tools (preventing infinite recursion).
    """

    async def start_check_loop(
        check_description: str,
        interval_seconds: float,
        max_iterations: int | None = None,
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
                agent_factory=agent_factory,
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

    return [start_check_loop]
