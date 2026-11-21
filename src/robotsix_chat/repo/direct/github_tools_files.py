"""File push, patch and commit tools for :func:`build_github_tools`, split out.

The tool closures below were extracted verbatim from
:func:`robotsix_chat.repo.direct.github_tools.build_github_tools` to keep that
factory manageable.  ``build_github_tools`` calls
:func:`build_github_tools_files` and splices the returned tools back into its
result list at their original positions, so the public tool set (names,
count, order) is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .github_tools import FILES_JSON_FORMS, validate_file_entries

if TYPE_CHECKING:
    from .client import DirectRepoClient


def build_github_tools_files(
    *,
    client: DirectRepoClient,
    assert_blocked_and_scoped: Callable[..., Awaitable[str | None]],
) -> list[Callable[..., Any]]:
    """Build and return the 3 file push, patch and commit tools.

    Returned in definition order; ``build_github_tools`` unpacks them
    positionally.  The closures capture only this function's parameters.
    """
    from robotsix_chat.common.unified_diff import apply_patch as _apply_patch

    async def push_direct_repo_branch(
        ticket_id: str,
        repo_full_name: str,
        branch_name: str,
        files_json: str,
        commit_message: str = "",
    ) -> str:
        """Push a new branch with file changes to a GitHub repository.

        Creates a new branch, writes the given files, and pushes them in a
        single commit.  The branch is created from the repository's default
        branch.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

        Args:
            ticket_id: The blocked ticket this branch addresses (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).  Used to verify
                BLOCKED state and for traceability in the commit/PR.
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            branch_name: Name for the new branch (e.g.
                ``"fix/20250624T020652Z-my-ticket-a1b2"``).
            files_json: JSON array of file entries to create or overwrite.
                Paths are relative to the repo root.  Each entry is an
                object with a ``path`` and exactly one content source:
                ``{"path": "...", "content": "..."}`` for text,
                ``{"path": "...", "content_b64": "..."}`` for base64-encoded
                bytes (binary files such as images), or
                ``{"path": "...", "local_path": "..."}`` to read the bytes
                from a file previously downloaded into the file-hub work
                directory (e.g. via ``file_hub_get``).  ``local_path`` must
                stay inside that directory — paths resolving outside it are
                rejected.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.

        Returns:
            A status message with the branch URL on success, or an error
            message describing why the push was refused or failed.

        """
        try:
            files: list[dict[str, str]] = json.loads(files_json)
        except json.JSONDecodeError, TypeError:
            return (
                "Error: files_json must be a valid JSON array of file "
                f"entries — {FILES_JSON_FORMS}."
            )

        if not isinstance(files, list):
            return "Error: files_json must be a JSON array."

        if entry_error := validate_file_entries(files):
            return entry_error

        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        # --- ensure changelog fragments end with a newline (text entries) ---
        for f in files:
            if (
                "content" in f
                and f.get("path", "").startswith("changelog.d/")
                and f["path"].endswith(".md")
                and not f.get("content", "").endswith("\n")
            ):
                f["content"] = f["content"] + "\n"

        # --- push the branch ---
        msg = commit_message or f"fix: address blocked ticket {ticket_id}"
        return await client.push_branch(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            files=files,
            commit_message=msg,
            ticket_id=ticket_id,
        )

    async def apply_patch_to_file(
        ticket_id: str,
        repo_full_name: str,
        branch_name: str,
        file_path: str,
        patch_content: str,
        commit_message: str = "",
        target_branch: str = "",
    ) -> str:
        """Push a patched file to a branch using a unified diff.

        By default (when *target_branch* is empty), fetches the current
        *file_path* from the repo's default branch, applies *patch_content*
        (a unified diff), and pushes the result as a commit on a **new**
        branch named *branch_name*.  This is the standard path for creating
        a fix PR from a blocked ticket and works regardless of the ticket's
        implement-cycle count.

        When *target_branch* is supplied, fetches and patches *file_path*
        from that **existing** branch and pushes the commit back onto it.
        Use this to update an open PR's head branch without creating a
        new branch, again with **no implement-cycle gate**.  This is the
        escape hatch for pushing a fix directly to an existing branch when
        the ticket is BLOCKED but ``patch_direct_repo_file`` refuses due to
        insufficient implement cycles.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** When called through the component roster the GitHub App
        installation scope check is bypassed.  For direct board-API calls,
        *repo_full_name* must be within the robotsix-mill GitHub App's
        installation scope.

        **Patch format:** Standard unified diff (as produced by ``diff -u``
        or ``git diff``)::

            --- a/path
            +++ b/path
            @@ -start,count +start,count @@
             context
            -removed
            +added

        Args:
            ticket_id: The blocked ticket this patch addresses (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            branch_name: Name for the new branch (e.g.
                ``"fix/20250624T020652Z-my-ticket-a1b2"``).  Ignored when
                *target_branch* is supplied.
            file_path: Path to the file to patch, relative to the repo
                root (e.g. ``"src/dashboard.js"``).
            patch_content: The unified diff to apply.  Must include at
                least one ``@@`` hunk header with context lines.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.
            target_branch: Optional.  When non-empty, push the patched file
                directly to this existing branch instead of creating a new
                branch.  The file is fetched from *target_branch* (not the
                default branch) so the patch applies to the branch's current
                state.  No implement-cycle gate is enforced.

        Returns:
            A status message with the branch URL on success, or an error
            message describing why the patch was refused or failed.

        """
        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        msg = commit_message or (
            f"fix: patch {file_path} for blocked ticket {ticket_id}"
        )

        if target_branch:
            # Push directly to the existing target branch —
            # fetches from that branch, patches, and pushes back.
            return await client.push_patched_file(
                repo_full_name=repo_full_name,
                branch_name=target_branch,
                file_path=file_path,
                patch_text=patch_content,
                commit_message=msg,
                ticket_id=ticket_id,
            )

        try:
            # Fetch the file from the default branch
            repo = await client._get_json(f"/repos/{repo_full_name}")
            default_branch: str = repo.get("default_branch", "main")

            original, _sha = await client.get_file_content(
                repo_full_name, file_path, ref=default_branch
            )
        except (RuntimeError, ValueError) as exc:
            return (
                f"Error fetching file '{file_path}' from "
                f"{repo_full_name}/{default_branch}: {exc}"
            )

        try:
            patched = _apply_patch(original, patch_content)
        except ValueError as exc:
            return f"Error applying patch to '{file_path}' in {repo_full_name}: {exc}"

        if patched == original:
            return (
                f"Patch applied to '{file_path}' in {repo_full_name} produced "
                f"no changes — the file may already be in the desired state."
            )

        return await client.push_branch(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            files=[{"path": file_path, "content": patched}],
            commit_message=msg,
            ticket_id=ticket_id,
        )

    async def push_patch_to_pr_branch(
        ticket_id: str,
        repo_full_name: str,
        pr_number: int,
        file_path: str = "",
        patch_content: str = "",
        commit_message: str = "",
        path: str = "",
        patch: str = "",
    ) -> str:
        """Push a patched commit to an existing pull request's head branch.

        Fetches *file_path* from the PR's head branch, applies *patch_content*
        (a unified diff), and pushes the result as a commit on the same branch.
        This is the standard path for updating a PR with a code change — no
        cycle-count gate, no new-branch creation.

        **Preconditions (all enforced by the tool):**
        1. Ticket MUST be in BLOCKED state.
        2. When called through the component roster (i.e. the
           ``component_request`` credential is available) the GitHub App
           installation scope check is bypassed — the mill already has its
           own GitHub access.  For direct board-API calls, *repo_full_name*
           MUST be in the GitHub App installation scope.
        3. The PR must exist and its head branch must belong to the same
           repository (*repo_full_name*) — cross-repo PR updates are refused.

        **Patch format:** Standard unified diff (as produced by ``diff -u``
        or ``git diff``)::

            --- a/path
            +++ b/path
            @@ -start,count +start,count @@
             context
            -removed
            +added

        Args:
            ticket_id: The blocked ticket this PR addresses (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to push to.
            file_path: Path to the file to patch, relative to the repo
                root (e.g. ``"src/dashboard.js"``).
            path: Alias of ``file_path``; the claude_sdk schema check
                rejected ``path=``/``patch=`` calls before the body ran
                (2026-09-17, correlation ab33c37a…).
            patch_content: The unified diff to apply.  Must include at
                least one ``@@`` hunk header with context lines.
            patch: Alias of ``patch_content``.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.

        Returns:
            A status message with the commit SHA on success, or an error
            message describing why the push was refused or failed.

        """
        for supplied, alias, name in (
            (file_path, path, "file_path"),
            (patch_content, patch, "patch_content"),
        ):
            if supplied.strip() and alias.strip() and supplied != alias:
                return f"Error: {name} and its alias disagree; pass only one of them."
        file_path = file_path or path
        patch_content = patch_content or patch
        if not file_path:
            return "Error: file_path is required."
        if not patch_content:
            return "Error: patch_content is required."

        # --- guard 1+2: BLOCKED + scope ---
        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        # --- guard 3: fetch PR and verify head branch ---
        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        head_info = pr.get("head", {})
        head_branch: str | None = head_info.get("ref")
        head_repo = head_info.get("repo", {})
        head_repo_full_name: str | None = head_repo.get("full_name")

        if not head_branch:
            return (
                f"Error: PR #{pr_number} in {repo_full_name} has no head "
                f"branch — cannot determine where to push."
            )

        if head_repo_full_name and head_repo_full_name != repo_full_name:
            return (
                f"Refused: PR #{pr_number} head branch '{head_branch}' belongs "
                f"to '{head_repo_full_name}', not '{repo_full_name}'. "
                f"Cross-repo PR updates are not permitted."
            )

        # --- push the patched commit ---
        msg = commit_message or (
            f"fix: patch {file_path} for blocked ticket {ticket_id} (PR #{pr_number})"
        )
        return await client.push_patched_file(
            repo_full_name=repo_full_name,
            branch_name=head_branch,
            file_path=file_path,
            patch_text=patch_content,
            commit_message=msg,
            ticket_id=ticket_id,
        )

    return [
        push_direct_repo_branch,
        apply_patch_to_file,
        push_patch_to_pr_branch,
    ]
