"""Reference-docs tool for the agent.

Lets the agent fetch documentation from allowlisted GitHub repos. Exposes
:func:`build_refdocs_tools` — a factory returning the LLM tool(s) that let
the chat agent read files from configured reference repos (primarily the
board-workflow reference repo). Returns no tools when refdocs is disabled, so
the chat runs exactly as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings, RefDocsSettings

logger = logging.getLogger(__name__)

__all__ = ["build_refdocs_tools"]


def build_refdocs_tools(
    settings: RefDocsSettings, direct_repo: DirectRepoSettings
) -> list[Callable[..., Any]]:
    """Return the refdocs tool(s) for the agent, or ``[]`` when unavailable."""
    if not settings.enabled:
        return []

    from .client import RefDocsClient

    client = RefDocsClient(settings, direct_repo)

    async def read_reference_doc(repo: str, path: str) -> str:
        """Read a single file from an allowlisted team reference repository.

        Use this to consult documentation in the board-workflow reference repo
        when deciding whether a ticket needs manual human action, or to look up
        any other documented conventions or processes from the team's reference
        repos.

        Args:
            repo: The ``owner/name`` of the GitHub repo (e.g.
                ``org/board-workflow``). Must be in the configured allowlist.
            path: The file path within the repo (e.g. ``docs/manual-states.md``).

        Returns:
            The file's text content, or a clear error message when the file
            cannot be fetched.

        """
        return await client.read_file(repo, path)

    async def list_reference_docs(repo: str, path: str = "") -> str:
        """List files and directories in a reference repository.

        Use this to discover what documentation is available in a reference
        repo before reading a specific file. For directories, the listing
        includes trailing slashes on subdirectory names.

        Args:
            repo: The ``owner/name`` of the GitHub repo (e.g.
                ``org/board-workflow``). Must be in the configured allowlist.
            path: The directory path to list, or ``""`` for the repo root.

        Returns:
            A formatted listing of the directory's contents, or a clear error
            message when the listing fails.

        """
        return await client.list_files(repo, path)

    return [read_reference_doc, list_reference_docs]
