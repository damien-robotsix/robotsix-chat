"""Broker responder that registers robotsix-chat as a component agent.

Imports ``robotsix_agent_comm.sdk.BrokeredAgent`` lazily (the optional
``broker`` extra) so the package stays importable without it — matching the
convention in ``broker_client.py``.  The responder dispatches three request
kinds — ``monitor``, ``config-get``, ``config-set`` — from a single
``on_request`` handler that inspects ``request.body["kind"]``, reads genuine
live runtime state, and mutates the live ``Settings`` via the validated
config contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
from typing import TYPE_CHECKING, Any

from robotsix_agent_comm.protocol import ConfigContractError

from robotsix_chat.component_agent.config_contract import (
    apply_config_update,
    describe_config,
    get_config_snapshot,
)

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventBus
    from robotsix_chat.chat.loops import CheckLoopRegistry
    from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["ComponentAgentResponder", "ComponentAgentResponderError"]


class ComponentAgentResponderError(RuntimeError):
    """Raised when the responder cannot start (e.g. missing broker extra)."""


class ComponentAgentResponder:
    """Async lifecycle manager for the embedded component-agent responder.

    Constructs the SDK ``BrokeredAgent`` lazily inside :meth:`start` so the
    module is importable without the ``broker`` extra.  The blocking broker
    serve loop runs in a thread via :func:`asyncio.to_thread`, keeping the
    async server responsive.

    All three request kinds (``monitor``, ``config-get``, ``config-set``)
    are dispatched by a single ``on_request`` handler that inspects
    ``request.body["kind"]``.

    Parameters
    ----------
        settings: The live application ``Settings`` instance.
        check_loop_registry: The process-wide check-loop registry.
        conversation_store: The process-wide conversation store.
        event_bus: The process-wide SSE event bus.

    """

    def __init__(
        self,
        settings: Settings,
        *,
        check_loop_registry: CheckLoopRegistry,
        conversation_store: ConversationStore,
        event_bus: EventBus,
    ) -> None:
        """Store references to live runtime objects; the SDK agent is built lazily."""
        self._settings = settings
        self._check_loop_registry = check_loop_registry
        self._conversation_store = conversation_store
        self._event_bus = event_bus
        self._serve_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the SDK agent, register the request handler, and begin serving.

        The blocking ``serve_forever()`` call is offloaded to a thread so the
        async event loop is never blocked.  Raises
        :class:`ComponentAgentResponderError` when the ``broker`` extra is
        not installed.
        """
        try:
            found = importlib.util.find_spec("robotsix_agent_comm")
        except (ValueError, ModuleNotFoundError):
            found = None
        if not found:
            raise ComponentAgentResponderError(
                "The broker extra (robotsix-agent-comm) is not installed. "
                "Reinstall with `uv sync --extra broker` or equivalent."
            )

        # Lazy import — guarded by the find_spec check above.
        from robotsix_agent_comm.sdk import BrokeredAgent  # noqa: I001

        ca = self._settings.component_agent

        self._agent = BrokeredAgent(
            ca.agent_id,
            broker_host=ca.broker_host,
            broker_port=ca.broker_port,
            broker_scheme=ca.broker_scheme,
            broker_token=ca.broker_token,
            timeout=ca.timeout,
            on_request=self._on_request,
        )

        # Offload the blocking serve loop to a thread.
        self._serve_task = asyncio.create_task(
            asyncio.to_thread(self._agent.serve_forever)
        )
        logger.info(
            "Component agent responder started "
            "(agent_id=%s, kinds=monitor,config-get,config-set)",
            ca.agent_id,
        )

    async def stop(self) -> None:
        """Tear down the responder — cancel the serve task and stop the agent."""
        if self._serve_task is not None:
            self._serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._serve_task
            self._serve_task = None
        if hasattr(self, "_agent"):
            try:
                self._agent.stop()
            except Exception:
                logger.debug(
                    "Agent stop raised (may be already stopped)", exc_info=True
                )
        logger.info("Component agent responder stopped.")

    # ------------------------------------------------------------------
    # on_request dispatcher
    # ------------------------------------------------------------------

    def _on_request(self, request: Any) -> Any:
        """Dispatch *request* to the handler named by ``request.body["kind"]``.

        Returns a ``Response`` on success or an ``Error`` on failure.  The
        SDK types are imported lazily so the body of this method is never
        executed before :meth:`start` succeeds.
        """
        from robotsix_agent_comm.protocol import Error, Response

        body = getattr(request, "body", {}) or {}
        kind = body.get("kind", "")

        try:
            if kind == "monitor":
                result = self._handle_monitor(body.get("payload", {}))
            elif kind == "config-get":
                result = self._handle_config_get(body.get("payload", {}))
            elif kind == "config-set":
                result = self._handle_config_set(body.get("payload", {}))
            else:
                return Error.to(
                    request,
                    code="UNKNOWN_KIND",
                    message=f"Unknown request kind: {kind!r}",
                    supported_kinds=["monitor", "config-get", "config-set"],
                )
        except ConfigContractError as exc:
            return Error.to(
                request,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            )
        except Exception as exc:
            logger.exception("Unhandled error in %s handler", kind)
            return Error.to(
                request,
                code="INTERNAL_ERROR",
                message=str(exc),
            )

        return Response.to(request, body=result)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_monitor(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return genuine live runtime telemetry.

        Returns a ``monitor`` body with:
        * ``check_loops``: running count + aggregate snapshot of all loops.
        * ``conversations``: stats from the conversation store.
        * ``event_bus``: subscriber counts.
        * ``settings``: redacted configuration snapshot.
        """
        # Check-loop telemetry.
        running = self._check_loop_registry.count_running()
        loops = self._check_loop_registry.snapshot()
        loop_summary: list[dict[str, object]] = []
        for info in loops:
            loop_summary.append(
                {
                    "id": info.id,
                    "session_id": info.session_id,
                    "prompt": info.prompt,
                    "interval_seconds": info.interval_seconds,
                    "status": info.status.value,
                    "iterations": info.iterations,
                    "max_iterations": info.max_iterations,
                    "last_result": info.last_result,
                    "error": info.error,
                    "stop_reason": info.stop_reason,
                    "reason": info.reason,
                }
            )

        # Conversation / EventBus stats.
        conv_stats = self._conversation_store.stats()
        event_subs = self._event_bus.subscriber_count()

        # Redacted settings snapshot.
        settings_snap = get_config_snapshot(self._settings)

        return {
            "check_loops": {
                "running": running,
                "total": len(loops),
                "loops": loop_summary,
            },
            "conversations": conv_stats,
            "event_bus": {
                "subscribers": event_subs,
            },
            "settings": settings_snap,
        }

    def _handle_config_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the redacted config snapshot and settable-key metadata."""
        return {
            "config": get_config_snapshot(self._settings),
            "settable": describe_config()["settable"],
        }

    def _handle_config_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a validated config update to the live ``Settings``.

        Expects ``payload["updates"]`` to be a mapping of dotted-path keys
        to new values.  Validation runs first; on failure the live instance
        is left untouched and a ``ConfigContractError`` is raised (framed by
        ``_on_request`` as an ``Error``).
        """
        updates = payload.get("updates")
        if not isinstance(updates, dict):
            raise ConfigContractError(
                code="INVALID_PAYLOAD",
                message="'updates' must be a dict of dotted-path key → value",
            )

        audit = apply_config_update(self._settings, updates)
        return {"applied": audit}
