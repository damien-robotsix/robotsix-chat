"""Delegation tool factory for the foreground chat agent.

Lets the foreground agent offload long-running work to a background sub-agent
via :func:`~robotsix_chat.chat.runner.spawn_subagent_task`. The tool returns
a task id immediately — the foreground reply is never blocked.

Usage::

    from robotsix_chat.chat.delegation import (
        build_delegation_tools,
        NullDeliveryChannel,
    )

    tools = build_delegation_tools(
        settings, registry, channel, client_id="browser-1",
    )
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

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
