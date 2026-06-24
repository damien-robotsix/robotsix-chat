"""Self-version-check tool for the agent.

Compares the running ``robotsix_chat.__version__`` against the latest
published GitHub release and warns the user when the deployment is behind.
Exposes :func:`build_version_check_tools` — a factory returning the LLM
tool(s) that let the chat agent self-report its version. Returns no tools
when disabled, so the chat runs exactly as before.

Note: the check is only meaningful when releases bump
``robotsix_chat.__version__`` in lockstep with the GitHub release tag.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import VersionCheckSettings

logger = logging.getLogger(__name__)

__all__ = ["build_version_check_tools"]


def build_version_check_tools(
    settings: VersionCheckSettings,
) -> list[Callable[..., Any]]:
    """Return the version-check tool for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from robotsix_chat import __version__ as current_version

    from .client import VersionCheckClient, compare_versions

    client = VersionCheckClient(settings)

    async def check_for_updates() -> str:
        """Report the running version and whether a newer release is available.

        Compares the installed ``robotsix-chat`` version against the latest
        published GitHub release of the configured repo.  When the running
        version is older, the summary includes a clear out-of-date warning
        and a link to the releases page so the user can see what changed.

        Note: this check is only meaningful when the release process bumps
        ``robotsix_chat.__version__`` in lockstep with the GitHub release tag.

        Args:
            (none)

        Returns:
            A human-readable summary string containing the current version,
            the latest release version (when available), and an up-to-date /
            out-of-date verdict.  On lookup failure, returns a graceful
            ``"Could not determine ..."`` message that includes the current
            version.

        """
        latest, source = await client.latest_version()
        if latest is None:
            return (
                f"Could not determine the latest version"
                f"{f': {source}' if source else '.'} "
                f"Current version is {current_version}."
            )

        current = current_version
        up_to_date = compare_versions(current, latest)

        base = f"Current version: {current}  |  Latest released version: {latest}"
        if up_to_date:
            return f"{base}  —  You are running the latest version."

        release_url = (
            f"{client._base_url.replace('/api.', '/')}/{settings.repo}/releases/latest"
        )
        return (
            f"{base}\n\n"
            f"⚠️  This deployment is OUT OF DATE. "
            f"Version {latest} is available. "
            f"Please update to get the latest fixes and patches.\n"
            f"Release page: {release_url}"
        )

    return [check_for_updates]
