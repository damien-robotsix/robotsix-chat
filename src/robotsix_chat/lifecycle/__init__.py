"""Read-only deploy-lifecycle API tools for the agent.

Exposes :func:`build_lifecycle_tools` — a factory returning read-only LLM
tools that let the chat agent inspect the central-deploy lifecycle server:
list services, check service status and health, and read configuration and
environment (secrets are masked server-side).  Returns no tools when the
lifecycle integration is disabled, so the chat runs exactly as before.

Also exposes :func:`load_lifecycle_skill` which returns the component skill
markdown — a description of the lifecycle API surface, allowed operations,
and mutation endpoints that are deliberately excluded.  Inject this into the
agent's system prompt so the LLM knows what the tools can and cannot do.

Mutation endpoints (restart, redeploy, config/env write) are deliberately
absent from this tool set — the lifecycle component is read-only by design.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import LifecycleSettings

__all__ = ["build_lifecycle_tools", "load_lifecycle_skill"]


def load_lifecycle_skill() -> str:
    """Return the lifecycle component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parents[3] / "docs" / "lifecycle" / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_lifecycle_tools(
    settings: LifecycleSettings,
) -> list[Callable[..., Any]]:
    """Return the lifecycle tool(s) for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .client import LifecycleClient

    client = LifecycleClient(settings)

    async def list_lifecycle_services() -> str:
        """List all services managed by the deploy lifecycle server.

        Returns a directory of managed services — each entry includes the
        service name, current status, and health.  Use this to discover
        what services are registered and their overall state.

        Returns:
            A text listing of managed services and their status, or an
            error message when the lifecycle server is unreachable.

        """
        return await client.list_services()

    async def get_lifecycle_service_status(service_name: str) -> str:
        """Get the live status and health of a single managed service.

        Returns the service's runtime status (running, stopped, unhealthy,
        etc.) plus recent health-check history.  Use this to diagnose a
        specific service that appears degraded.

        Args:
            service_name: The service identifier as returned by
                ``list_lifecycle_services``.

        Returns:
            The service's status and health details, or an error message.

        """
        return await client.service_status(service_name)

    async def get_lifecycle_service_config(service_name: str) -> str:
        """Read the current configuration of a managed service.

        Returns a snapshot of the service's live configuration.  Secret
        values are already masked as ``***`` server-side — this endpoint
        never exposes credentials.

        Args:
            service_name: The service identifier as returned by
                ``list_lifecycle_services``.

        Returns:
            The service's configuration (secrets redacted), or an error
            message.

        """
        return await client.service_config(service_name)

    async def get_lifecycle_service_env(service_name: str) -> str:
        """Read the environment variables of a managed service.

        Returns the service's runtime environment.  Secret values are
        already masked as ``***`` server-side — this endpoint never
        exposes credentials.

        Args:
            service_name: The service identifier as returned by
                ``list_lifecycle_services``.

        Returns:
            The service's environment (secrets redacted), or an error
            message.

        """
        return await client.service_env(service_name)

    async def watch_service_redeploy(
        service_name: str,
        max_wait_seconds: float = 300.0,
        poll_interval_seconds: float = 15.0,
    ) -> str:
        """Watch a lifecycle-managed service until a redeploy is detected.

        Takes a snapshot of the service configuration and polls every
        *poll_interval_seconds* until the config changes (indicating a
        redeploy) or *max_wait_seconds* elapses.  Use this after a fix
        is merged into a component's repo — call it to block until the
        redeploy completes so you do not keep retrying against the stale
        deployment.

        This tool will block the agent's turn for up to
        *max_wait_seconds* — only call it when waiting for a redeploy is
        the right next action.  If the timeout expires without a change,
        the tool returns a message suggesting the operator trigger a
        manual redeploy via the central-deploy dashboard.

        Args:
            service_name: The lifecycle-registered service to watch
                (e.g. ``"robotsix-mill"``).
            max_wait_seconds: Maximum time to wait (default 300 s).
                The tool returns early as soon as a redeploy is detected.
            poll_interval_seconds: Seconds between config polls
                (default 15 s, minimum 5 s).

        Returns:
            A summary: redeploy detected (with current status), or a
            timeout message with the recommended next action.

        """
        return await client.watch_service_redeploy(
            service_name,
            max_wait_seconds=max_wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    return [
        list_lifecycle_services,
        get_lifecycle_service_status,
        get_lifecycle_service_config,
        get_lifecycle_service_env,
        watch_service_redeploy,
    ]
