"""Component agent client tools for the chat LLM.

Exposes :func:`build_component_tools` — a factory returning LLM tools that let
the chat agent enumerate, inspect, and configure remote component agents over
direct HTTP. Returns no tools when the component client is disabled or when
the ``components`` allowlist is empty.

The tools are plain async callables; robotsix-llmio converts them into tools for
the underlying agent (the claude-sdk tool loop, or pydantic-ai function tools).
"""

from __future__ import annotations

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

    from .client import ComponentAgentClient

    client = ComponentAgentClient(settings)
    allowed_urls = {c.base_url for c in settings.components}

    def _known_urls_msg() -> str:
        urls = sorted(allowed_urls)
        if not urls:
            return "No component agents are configured."
        return f"Known component agent URLs: {', '.join(urls)}"

    def _validate_base_url(base_url: str) -> str | None:
        """Return an error message if *base_url* is not in the allowlist."""
        if base_url not in allowed_urls:
            return f"Unknown component agent '{base_url}'. {_known_urls_msg()}"
        return None

    async def list_component_agents() -> str:
        """List the known component agents and their supported request kinds.

        Returns the configured component agents (base URL + label) and the
        request kinds each agent supports (monitor, config-get, config-set).

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
            lines.append(f"  - {c.base_url}{label}")
        lines.append("")
        lines.append("Supported request kinds: " + ", ".join(_SUPPORTED_KINDS))
        return "\n".join(lines)

    async def get_component_telemetry(base_url: str) -> str:
        """Fetch live telemetry (monitor) from a component agent.

        Returns the agent's runtime stats: check-loop counts, conversation
        store summaries, event bus subscriber counts, and a redacted settings
        snapshot.

        Args:
            base_url: The base URL of the component agent to query
                (e.g. ``"http://comp-1:8090"``).

        Returns:
            The agent's monitor snapshot, or an error message.

        """
        err = _validate_base_url(base_url)
        if err:
            return err
        return await client.monitor(base_url)

    async def get_component_config(base_url: str) -> str:
        """Read the current configuration of a component agent.

        Returns a redacted snapshot of the agent's live config plus metadata
        about which keys are settable.

        Args:
            base_url: The base URL of the component agent to query
                (e.g. ``"http://comp-1:8090"``).

        Returns:
            The agent's config snapshot and settable-key metadata, or an error.

        """
        err = _validate_base_url(base_url)
        if err:
            return err
        return await client.config_get(base_url)

    async def set_component_config(base_url: str, updates: dict[str, Any]) -> str:
        """Update configuration on a running component agent.

        Sends a validated config update to the agent. The agent applies the
        changes to its live settings and returns an audit of what was changed.
        Validation failures from the target are surfaced back as clear error
        messages.

        Args:
            base_url: The base URL of the component agent to configure
                (e.g. ``"http://comp-1:8090"``).
            updates: A mapping of dotted-path config keys to new values
                (e.g. ``{"server.port": 8080}``).

        Returns:
            An audit of applied changes, or an error message.

        """
        err = _validate_base_url(base_url)
        if err:
            return err
        return await client.config_set(base_url, updates)

    return [
        list_component_agents,
        get_component_telemetry,
        get_component_config,
        set_component_config,
    ]
