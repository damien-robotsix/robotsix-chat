"""Component agent client tools for the chat LLM.

Exposes :func:`build_component_tools` — a factory returning LLM tools that let
the chat agent enumerate, inspect, and configure remote component agents over
the agent-comm broker. Returns no tools when the component client is disabled or
when the ``broker`` extra (robotsix-agent-comm) is absent, so the chat runs
exactly as before.

The tools are plain async callables; robotsix-llmio converts them into tools for
the underlying agent (the claude-sdk tool loop, or pydantic-ai function tools).
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import ComponentClientSettings

logger = logging.getLogger(__name__)

__all__ = ["build_component_tools"]

_SUPPORTED_KINDS = ["monitor", "config-get", "config-set"]


def build_component_tools(
    settings: ComponentClientSettings,
) -> list[Callable[..., Any]]:
    """Return the component agent tools for the agent, or ``[]`` when unavailable."""
    if not settings.enabled:
        return []
    if importlib.util.find_spec("robotsix_agent_comm") is None:
        logger.warning(
            "component_client.enabled is true but the 'broker' extra "
            "(robotsix-agent-comm) is not installed — component agent tools "
            "are unavailable. Install robotsix-chat[broker]."
        )
        return []

    from .client import ComponentAgentClient

    client = ComponentAgentClient(settings)
    allowed_ids = {c.agent_id for c in settings.components}

    def _known_ids_msg() -> str:
        ids = sorted(allowed_ids)
        if not ids:
            return "No component agents are configured."
        return f"Known component agents: {', '.join(ids)}"

    def _validate_agent_id(agent_id: str) -> str | None:
        """Return an error message if *agent_id* is not in the allowlist."""
        if agent_id not in allowed_ids:
            return f"Unknown component agent '{agent_id}'. {_known_ids_msg()}"
        return None

    async def list_component_agents() -> str:
        """List the known component agents and their supported request kinds.

        Returns the configured component agents (id + label) and the request
        kinds each agent supports (monitor, config-get, config-set).

        Returns:
            A text listing of known agents and supported operations.

        """
        if not settings.components:
            return (
                "No component agents are configured. "
                "Add entries to `component_client.components` in the config."
            )
        lines: list[str] = [
            "Configured component agents:",
            "",
        ]
        for c in settings.components:
            label = f" ({c.label})" if c.label else ""
            lines.append(f"  - {c.agent_id}{label}")
        lines.append("")
        lines.append("Supported request kinds: " + ", ".join(_SUPPORTED_KINDS))
        return "\n".join(lines)

    async def get_component_telemetry(agent_id: str) -> str:
        """Fetch live telemetry (monitor) from a component agent.

        Returns the agent's runtime stats: check-loop counts, conversation
        store summaries, event bus subscriber counts, and a redacted settings
        snapshot.

        Args:
            agent_id: The broker agent id of the component to query.

        Returns:
            The agent's monitor snapshot, or an error message.

        """
        err = _validate_agent_id(agent_id)
        if err:
            return err
        return await client.monitor(agent_id)

    async def get_component_config(agent_id: str) -> str:
        """Read the current configuration of a component agent.

        Returns a redacted snapshot of the agent's live config plus metadata
        about which keys are settable.

        Args:
            agent_id: The broker agent id of the component to query.

        Returns:
            The agent's config snapshot and settable-key metadata, or an error.

        """
        err = _validate_agent_id(agent_id)
        if err:
            return err
        return await client.config_get(agent_id)

    async def set_component_config(agent_id: str, updates: dict[str, Any]) -> str:
        """Update configuration on a running component agent.

        Sends a validated config update to the agent. The agent applies the
        changes to its live settings and returns an audit of what was changed.
        Validation failures from the target are surfaced back as clear error
        messages.

        Args:
            agent_id: The broker agent id of the component to configure.
            updates: A mapping of dotted-path config keys to new values
                (e.g. ``{"server.port": 8080}``).

        Returns:
            An audit of applied changes, or an error message.

        """
        err = _validate_agent_id(agent_id)
        if err:
            return err
        return await client.config_set(agent_id, updates)

    return [
        list_component_agents,
        get_component_telemetry,
        get_component_config,
        set_component_config,
    ]
