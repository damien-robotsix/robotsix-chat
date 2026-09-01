"""Deploy-lifecycle API tools for the agent.

Exposes :func:`build_lifecycle_tools` — a factory returning LLM tools
that let the chat agent inspect and (when permitted) mutate the
central-deploy lifecycle server: list services, check service status and
health, read environment, restart services, and update service
environment (secrets are masked server-side on reads).  Configuration
management is owned by each component internally — no tools are provided
for reading or writing service configuration through central-deploy.
Returns no tools when the lifecycle integration is disabled, so the chat
runs exactly as before.

Also exposes :func:`load_lifecycle_skill` which returns the component skill
markdown — a description of the lifecycle API surface, allowed operations,
and mutation endpoints that require the deploy server's per-repo access
toggle.  Inject this into the agent's system prompt so the LLM knows what
the tools can and cannot do.

Mutation endpoints (restart, env write) are available as tools and
succeed or fail based on the deploy server's per-repo access toggle
for the calling component.
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
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_lifecycle_tools(
    settings: LifecycleSettings,
    base_url: str = "",
) -> list[Callable[..., Any]]:
    """Return the lifecycle tool(s) for the agent, or ``[]`` when disabled.

    *base_url* is the canonical ``central_deploy.url`` (the former
    ``lifecycle.base_url`` was retired).
    """
    if not settings.enabled:
        return []

    from .client import LifecycleClient

    client = LifecycleClient(settings, base_url)

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

    async def restart_lifecycle_service(service_name: str) -> str:
        """Restart a lifecycle-managed service.

        Sends a restart request to the deploy server.  The restart is
        permitted only when the deploy server's per-repo access toggle
        is enabled for this component — otherwise the call returns a
        403 error.  For restarting the agent's own service when the
        toggle is not enabled, use ``self_restart`` instead.

        Args:
            service_name: The service identifier as returned by
                ``list_lifecycle_services`` (e.g. ``"chat"``).

        Returns:
            The restart result or an error message (including 403 when
            the per-repo access toggle is not enabled).

        """
        return await client.restart_service(service_name)

    async def redeploy_lifecycle_service(service_name: str, image_ref: str = "") -> str:
        """Redeploy a lifecycle-managed service.

        Sends a redeploy request to the deploy server.  When
        *image_ref* is set, the deploy server is instructed to pull
        that specific image (e.g. a ``sha-<commit>`` tag) rather than
        the service's default tag — this avoids restarting on a stale
        image when the build pipeline has not yet finished publishing
        the latest ``:main`` tag.  The redeploy is permitted only when
        the deploy server's per-repo access toggle is enabled for this
        component — otherwise the call returns a 403 error.

        Args:
            service_name: The service identifier as returned by
                ``list_lifecycle_services`` (e.g. ``"file-hub"``).
            image_ref: Optional specific image reference (tag, digest,
                or ``sha-<commit>``) to pull.  When empty the deploy
                server uses the service's default image tag.

        Returns:
            The redeploy result or an error message (including 403 when
            the per-repo access toggle is not enabled).

        """
        return await client.redeploy_service(service_name, image_ref=image_ref)

    async def self_restart() -> str:
        """Restart the agent's own service.

        Restarts this service via the deploy server's chat-agent restart
        endpoint, naming this service through the configured
        ``lifecycle.service_name``.  Use this after a deploy that changed
        the agent's own capabilities (new component, tool, skill, or
        permission) so the new capability is picked up.

        Only call this for the agent's own service — it cannot restart
        other managed services.  For restarting other services, use
        ``restart_lifecycle_service``.

        Returns:
            The restart result or an error message (including when
            ``lifecycle.service_name`` is not configured).

        """
        return await client.self_restart()

    async def update_lifecycle_service_env(
        service_name: str, env: dict[str, Any]
    ) -> str:
        """Update the environment variables of a lifecycle-managed service.

        Sends new environment values to the deploy server.  Secrets are
        handled server-side — never pass plaintext credentials.  The
        update is permitted only when the deploy server's per-repo
        access toggle is enabled for this component — otherwise the
        call returns a 403 error.

        Args:
            service_name: The service identifier as returned by
                ``list_lifecycle_services`` (e.g. ``"chat"``).
            env: A dictionary of environment variable key/value pairs
                to update (not a full replacement — only the provided
                keys are changed).

        Returns:
            The update result or an error message (including 403 when
            the per-repo access toggle is not enabled).

        """
        return await client.update_service_env(service_name, env)

    async def verify_lifecycle_deployment(
        service_name: str,
        expected_image_ref: str = "",
        poll_timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 15.0,
    ) -> str:
        """Verify that a lifecycle-managed service is deployed and healthy.

        Polls the deploy server for *service_name*'s status until the
        service reports healthy and, when *expected_image_ref* is
        provided, the running image matches.  Use this after merging a
        PR or triggering a redeploy to confirm the new version is live
        before closing the associated ticket.

        *expected_image_ref* can be any substring of the running image
        identifier — a tag (``:main``), a digest prefix
        (``sha256:abc123``), or a full registry reference
        (``ghcr.io/owner/repo:main``).

        Args:
            service_name: The service identifier (e.g. ``"chat"``).
            expected_image_ref: Optional image reference to match
                against the running image.  Leave empty to verify
                health only.
            poll_timeout_seconds: Maximum time to wait (default 300 s).
            poll_interval_seconds: Time between polls (default 15 s).

        Returns:
            A JSON verdict with ``verified``, ``detail``, and the
            last known service status.

        """
        return await client.verify_deployment(
            service_name=service_name,
            expected_image_ref=expected_image_ref,
            poll_timeout_seconds=poll_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    return [
        list_lifecycle_services,
        get_lifecycle_service_status,
        get_lifecycle_service_env,
        restart_lifecycle_service,
        redeploy_lifecycle_service,
        update_lifecycle_service_env,
        self_restart,
        verify_lifecycle_deployment,
    ]
