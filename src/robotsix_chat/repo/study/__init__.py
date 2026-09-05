"""Repo-study tools — temporary local clones the agent can read.

Exposes :func:`build_repo_study_tools`, a factory returning read-only LLM
tools that let the chat agent fetch a GitHub repository snapshot (tarball,
no ``git`` binary) into a temporary workspace and study it locally: list
files, read them with line numbers, and regex-search across the tree.
Returns no tools when repo_study is disabled, so the chat runs exactly as
before.

When *diagnostic_store* is provided, every ``fetch_repo_for_study`` failure
is automatically recorded as a ``CLONE_TARGET`` diagnostic event so the
:class:`~robotsix_chat.diagnostics.fixes.RecurrenceDetector` and
:func:`check_recurring_categories` tool can surface systemic clone issues.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings, RepoStudySettings
    from robotsix_chat.diagnostics.store import DiagnosticStore

logger = logging.getLogger(__name__)

__all__ = ["build_repo_study_tools"]


def build_repo_study_tools(
    settings: RepoStudySettings,
    direct_repo: DirectRepoSettings,
    *,
    diagnostic_store: DiagnosticStore | None = None,
) -> list[Callable[..., Any]]:
    """Return the repo-study tools for the agent, or ``[]`` when disabled."""
    if not settings.enabled:
        return []

    from .workspace import WorkspaceError, WorkspaceManager

    manager = WorkspaceManager(settings, direct_repo)

    async def _resolve_repo(repo: str) -> tuple[str | None, str]:
        """Turn a mill ``repo_id`` or ``owner/repo`` into ``(full_name, error)``.

        ``owner/repo`` passes through untouched.  A bare name is looked up
        in the mill repo registry (``GET /repos``, via
        :class:`~robotsix_chat.repo.direct.board_client.BoardClient`) and,
        failing that, matched by repository name against the GitHub App
        installation's repositories.  Never guesses an owner.
        """
        text = repo.strip()
        if not text or "/" in text:
            return (text or None), ""
        candidates: list[str] = []
        if direct_repo.board_api_base_url.strip():
            from robotsix_chat.repo.direct.board_client import BoardClient

            try:
                resolved = await BoardClient(direct_repo).resolve_repo_full_name(text)
            except Exception as exc:  # defensive — never break the tool
                logger.warning("repo_study: mill registry lookup failed: %s", exc)
                resolved = None
            if resolved:
                return resolved, ""
        if (
            direct_repo.github_app_id
            and direct_repo.github_app_private_key.get_secret_value()
            and direct_repo.github_app_installation_id
        ):
            from robotsix_chat.repo.direct.client import DirectRepoClient

            try:
                installed = await DirectRepoClient(
                    direct_repo
                ).list_installation_repos()
            except Exception as exc:
                logger.warning("repo_study: installation repo list failed: %s", exc)
                installed = []
            candidates = [
                full
                for full in installed
                if full.rsplit("/", 1)[-1].lower() == text.lower()
            ]
            if len(candidates) == 1:
                return candidates[0], ""
        return None, (
            f"Error: {repo!r} is not an 'owner/name' full name, not a registered "
            "mill repo_id, and does not uniquely match a repository in the "
            "GitHub App installation"
            + (f" (candidates: {', '.join(sorted(candidates))})" if candidates else "")
            + ".  Call resolve_repo(repo_id) or pass the exact owner/name — "
            "do not guess an organisation."
        )

    async def fetch_repo_for_study(
        repo: str = "", ref: str = "", repo_id: str = "", full_name: str = ""
    ) -> str:
        """Download a GitHub repo snapshot into a temporary local workspace.

        Use this when you need to actually study a codebase — follow imports,
        search across files, read implementations — rather than fetch a single
        known document. The snapshot is read-only and temporary (it expires
        automatically); nothing you do here touches the real repository.
        Private repos work when they are in the configured GitHub App
        installation scope; public repos always work.

        Args:
            repo: The ``owner/name`` GitHub repository full name, or a mill
                ``repo_id`` (e.g. ``"robotsix-central-deploy"``) — resolved
                to ``owner/name`` through the mill repo registry / the
                GitHub App installation.  Never pass a guessed owner.
            ref: Optional branch, tag, or commit SHA (default branch when
                empty).
            repo_id: Alias for ``repo`` — pass one of the two.
            full_name: Alias for ``repo`` — pass one of the two.

        Returns:
            A summary with the workspace id to pass to the other repo-study
            tools, or a clear error message.

        """
        # ``repo_id`` / ``full_name`` exist because sibling tools
        # (resolve_repo, the mill ticket tools) use those names, so agents
        # reach for them here too — and used to get a hard "Additional
        # properties are not allowed" validation failure, one wasted turn
        # per guess (live incident 2026-09-05, correlation 630aee98…).
        repo = repo or full_name or repo_id
        if not repo:
            return (
                "Error: pass the repository as repo — an 'owner/name' full "
                "name or a mill repo_id."
            )
        resolved_full_name, resolve_error = await _resolve_repo(repo)
        if resolve_error:
            if diagnostic_store is not None:
                diagnostic_store.record_event(
                    category="CLONE_TARGET",
                    message=f"Repo fetch failed for {repo}: {resolve_error}",
                    details={"repo": repo, "ref": ref, "error": resolve_error},
                )
            return resolve_error
        repo = resolved_full_name or repo
        try:
            return await manager.fetch(repo, ref)
        except WorkspaceError as exc:
            if diagnostic_store is not None:
                diagnostic_store.record_event(
                    category="CLONE_TARGET",
                    message=f"Repo fetch failed for {repo}: {exc}",
                    details={"repo": repo, "ref": ref, "error": str(exc)},
                )
            return f"Error: {exc}"

    async def list_repo_files(
        workspace_id: str,
        glob: str = "**/*",
        path: str = "",
        max_entries: int = 500,
    ) -> str:
        """List files in a fetched repo workspace.

        Args:
            workspace_id: The id returned by ``fetch_repo_for_study``.
            glob: Workspace-relative glob filter (e.g. ``src/**/*.py``).
            path: Workspace-relative directory to list (e.g. ``changelog.d``).
                Combines with *glob* when both are given.
            max_entries: Cap on the number of entries returned.

        Returns:
            One ``path (size bytes)`` line per file, or an error message.

        """
        # ``path`` exists because every sibling tool (read_repo_file,
        # write_repo_file, …) takes one, so agents reach for it here too —
        # and used to get a hard "Additional properties are not allowed
        # ('path' was unexpected)" validation failure. That cost a wasted
        # turn each time and generated recurring tool_error tickets. A
        # directory is just a glob prefix, so accept it directly.
        effective_glob = glob or "**/*"
        if path:
            prefix = path.strip("/")
            if prefix:
                effective_glob = f"{prefix}/{effective_glob}"
        try:
            return manager.list_files(workspace_id, effective_glob, max_entries)
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
        pattern: str = "",
        glob: str = "**/*",
        max_matches: int = 50,
        query: str = "",
    ) -> str:
        """Regex-search across the files of a fetched repo workspace.

        Args:
            workspace_id: The id returned by ``fetch_repo_for_study``.
            pattern: Python regular expression, matched per line.
            glob: Workspace-relative glob restricting which files to search.
            max_matches: Cap on the number of matches returned.
            query: Alias for ``pattern`` — pass one of the two.

        Returns:
            ``path:line: text`` matches, or an error message.

        """
        # ``query`` is what agents guess when they haven't seen the schema
        # (live incident 2026-09-05); accept it instead of burning a turn.
        pattern = pattern or query
        if not pattern:
            return "Error: pass the regular expression as pattern."
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
