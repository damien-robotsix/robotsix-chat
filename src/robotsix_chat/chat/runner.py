"""Background sub-agent runner — spawns a same-tier agent off the request path.

The foreground chat agent (:class:`~robotsix_chat.llm.agent.LlmioChatAgent`)
delegates long-running prompts to this module. It creates a fresh agent via
the injected *agent_factory* (defaulting to
:func:`~robotsix_chat.chat.server.create_agent_from_settings`), runs the
delegated prompt to completion, updates the shared
:class:`~robotsix_chat.chat.tasks.TaskRegistry`, and pushes notification frames
through the injected ``DeliveryChannel``.

The delivery channel is abstracted behind a small :class:`typing.Protocol`
because the concrete SSE events registry (Ticket 1) had not landed when this
module was written. When it lands, its concrete registry satisfies this
Protocol by duck typing — no change needed here.

Usage::

    task_id = spawn_subagent_task(
        client_id="browser-1",
        prompt="Summarise the last 3 conversations",
        settings=settings,
        registry=task_registry,
        channel=events_channel,
    )
    # task_id returned immediately; the sub-agent runs in the background.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

from robotsix_chat.chat.server import ChatAgent, create_agent_from_settings
from robotsix_chat.chat.tasks import TaskRegistry
from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Delivery-channel abstraction (Protocol, so any concrete events registry
# from Ticket 1 satisfies it by duck typing — no explicit ``implements``).
# ---------------------------------------------------------------------------


class DeliveryChannel(Protocol):
    """Structural interface for pushing notification frames to a client.

    When Ticket 1's concrete SSE events registry lands, it (or a thin adapter)
    satisfies this protocol without any change to this module.
    """

    async def publish(self, client_id: str, frame: dict[str, Any]) -> None:
        """Deliver *frame* to the owner of *client_id*."""
        ...


# ---------------------------------------------------------------------------
# Frame builders — small helper functions so frame shapes are testable.
# ---------------------------------------------------------------------------


def task_started_frame(task_id: str, prompt: str) -> dict[str, Any]:
    """Return a ``task_started`` notification frame."""
    return {"type": "task_started", "task_id": task_id, "prompt": prompt}


def task_completed_frame(task_id: str, result: str) -> dict[str, Any]:
    """Return a ``task_completed`` notification frame."""
    return {"type": "task_completed", "task_id": task_id, "result": result}


def task_failed_frame(task_id: str, error: str) -> dict[str, Any]:
    """Return a ``task_failed`` notification frame."""
    return {"type": "task_failed", "task_id": task_id, "error": error}


# ---------------------------------------------------------------------------
# Default agent factory
# ---------------------------------------------------------------------------


def _default_agent_factory(settings: Settings) -> ChatAgent:
    """Build a same-tier agent via :func:`create_agent_from_settings`.

    Kept as a module-level function so tests can reference it and assert the
    runner never hard-codes a model tier.
    """
    return create_agent_from_settings(settings=settings)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def spawn_subagent_task(
    *,
    client_id: str,
    prompt: str,
    settings: Settings,
    registry: TaskRegistry,
    channel: DeliveryChannel,
    agent_factory: Callable[[Settings], ChatAgent] = _default_agent_factory,
) -> str:
    """Schedule a delegated sub-agent prompt; return the task id immediately.

    The sub-agent is constructed via *agent_factory* using the provided
    *settings* — so the model tier always equals the foreground tier
    (``settings.llmio_model_level``). The worker coroutine runs off the
    request path with a strong reference held by *registry*.

    On completion the registry is updated and a notification frame is pushed
    through *channel*. On failure the registry is updated with the error and
    a failure frame is pushed (a channel error is logged, not propagated).
    """
    # Race-free handshake: the worker coroutine needs its own task_id, but the
    # id is only known after ``registry.register`` returns.  ``asyncio.create_task``
    # schedules the coroutine but does not run it until the next await point,
    # so we resolve the future before the worker reads it.
    id_future: asyncio.Future[str] = asyncio.Future()

    async def _worker() -> None:
        task_id = await id_future
        try:
            agent = agent_factory(settings)
            result_text = "".join([chunk async for chunk in agent.stream(prompt)])
            registry.complete(task_id, result_text)
            try:
                await channel.publish(
                    client_id, task_completed_frame(task_id, result_text)
                )
            except Exception:
                logger.exception(
                    "DeliveryChannel.publish failed for task %s (already completed)",
                    task_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Sub-agent task %s failed", task_id)
            registry.fail(task_id, str(exc))
            try:
                await channel.publish(client_id, task_failed_frame(task_id, str(exc)))
            except Exception:
                logger.exception(
                    "DeliveryChannel.publish failed for failed task %s", task_id
                )

    task = asyncio.create_task(_worker())
    task_id = registry.register(client_id, prompt, task)
    id_future.set_result(task_id)
    return task_id
