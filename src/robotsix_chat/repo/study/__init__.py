"""Repo-study tools — temporary local clones the agent can read.

Exposes :func:`build_repo_study_tools`, a factory returning read-only LLM
tools that let the chat agent fetch a GitHub repository snapshot (tarball,
no ``git`` binary) into a temporary workspace and study it locally: list
files, read them with line numbers, and regex-search across the tree.
Returns no tools when repo_study is disabled, so the chat runs exactly as
before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings, RepoStudySettings

logger = logging.getLogger(__name__)

__all__ = ["build_repo_study_tools"]


def build_repo_study_tools(
    settings: RepoStudySettings,
    direct_repo: DirectRepoSettings,
) -> list[Callable[..., Any]]:
    """Return the repo-study tools for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .workspace import WorkspaceError, WorkspaceManager

    manager = WorkspaceManager(settings, direct_repo)

    async def fetch_repo_for_study(repo: str, ref: str = "") -> str:
        """Download a GitHub repo snapshot into a temporary local workspace.

        Use this when you need to actually study a codebase — follow imports,
        search across files, read implementations — rather than fetch a single
        known document. The snapshot is read-only and temporary (it expires
        automatically); nothing you do here touches the real repository.
        Private repos work when they are in the configured GitHub App
        installation scope; public repos always work.

        Args:
            repo: The ``owner/name`` GitHub repository full name.
            ref: Optional branch, tag, or commit SHA (default branch when
                empty).

        Returns:
            A summary with the workspace id to pass to the other repo-study
            tools, or a clear error message.

        """
        try:
            return await manager.fetch(repo, ref)
        except WorkspaceError as exc:
            return f"Error: {exc}"

    async def list_repo_files(
        workspace_id: str, glob: str = "**/*", max_entries: int = 500
    ) -> str:
        """List files in a fetched repo workspace.

        Args:
            workspace_id: The id returned by ``fetch_repo_for_study``.
            glob: Workspace-relative glob filter (e.g. ``src/**/*.py``).
            max_entries: Cap on the number of entries returned.

        Returns:
            One ``path (size bytes)`` line per file, or an error message.

        """
        try:
            return manager.list_files(workspace_id, glob, max_entries)
        except WorkspaceError as exc:
            return f"Error: {exc}"

    async def read_repo_file(
        workspace_id: str, path: str, start_line: int = 1, max_lines: int = 400
    ) -> str:
        """Read a file from a fetched repo workspace, with line numbers.

        Args:
            workspace_id: The id returned by ``fetch_repo_for_study``.
            path: Workspace-relative file path.
            start_line: 1-based first line to return.
            max_lines: Maximum number of lines to return.

        Returns:
            Line-numbered, tab-separated file content, or an error message.

        """
        try:
            return manager.read_file(workspace_id, path, start_line, max_lines)
        except WorkspaceError as exc:
            return f"Error: {exc}"

    async def search_repo_files(
        workspace_id: str,
        pattern: str,
        glob: str = "**/*",
        max_matches: int = 50,
    ) -> str:
        """Regex-search across the files of a fetched repo workspace.

        Args:
            workspace_id: The id returned by ``fetch_repo_for_study``.
            pattern: Python regular expression, matched per line.
            glob: Workspace-relative glob restricting which files to search.
            max_matches: Cap on the number of matches returned.

        Returns:
            ``path:line: text`` matches, or an error message.

        """
        try:
            return manager.search(workspace_id, pattern, glob, max_matches)
        except WorkspaceError as exc:
            return f"Error: {exc}"

    async def delete_workspace_artifact(workspace_id: str, path: str) -> str:
        """Delete a single file or directory from a fetched repo workspace.

        Use this to remove a specific artifact (file or subdirectory) without
        dropping the entire workspace.

        Args:
            workspace_id: The id returned by ``fetch_repo_for_study``.
            path: Workspace-relative path to the file or directory to delete.

        Returns:
            A confirmation, or an error message.

        """
        try:
            return manager.delete_artifact(workspace_id, path)
        except WorkspaceError as exc:
            return f"Error: {exc}"

    async def drop_repo_workspace(workspace_id: str) -> str:
        """Delete a fetched repo workspace as soon as you are done with it.

        Workspaces also expire automatically, but dropping them promptly
        frees disk on the persistent volume.

        Args:
            workspace_id: The id returned by ``fetch_repo_for_study``.

        Returns:
            A confirmation, or an error message.

        """
        try:
            return manager.drop(workspace_id)
        except WorkspaceError as exc:
            return f"Error: {exc}"

    return [
        fetch_repo_for_study,
        list_repo_files,
        read_repo_file,
        search_repo_files,
        delete_workspace_artifact,
        drop_repo_workspace,
    ]
