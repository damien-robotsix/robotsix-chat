"""PR lifecycle tools for :func:`build_github_tools`, split out.

Open / update / merge / close / auto-merge tools plus the CI merge gate.

The tool closures below were extracted verbatim from
:func:`robotsix_chat.repo.direct.github_tools.build_github_tools` to keep that
factory manageable.  ``build_github_tools`` calls
:func:`build_github_tools_pr_lifecycle` and splices the returned tools back into its
result list at their original positions, so the public tool set (names,
count, order) is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .actions_client import ActionsClient, summarize_check_runs
from .github_tools import (
    FILES_JSON_CHANGESET_FORMS,
    FILES_JSON_FORMS,
    _confirmation_echo,
    check_simple_pr_file_safety,
    validate_changeset_entries,
    validate_file_entries,
)

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

    from .client import DirectRepoClient


def build_github_tools_pr_lifecycle(
    *,
    client: DirectRepoClient,
    settings: DirectRepoSettings,
    component_request: Callable[..., Any] | None,
    assert_blocked_and_scoped: Callable[..., Awaitable[str | None]],
    assert_in_scope: Callable[..., Awaitable[str | None]],
) -> list[Callable[..., Any]]:
    """Build and return the 9 PR lifecycle tools (open/update/merge/close/auto-merge).

    Returned in definition order; ``build_github_tools`` unpacks them
    positionally.  The closures capture only this function's parameters.
    """

    async def open_direct_repo_pr(
        ticket_id: str,
        repo_full_name: str,
        branch_name: str,
        title: str,
        body: str = "",
    ) -> str:
        """Open a pull request from a branch.

        Opens a standard PR (not draft) in a reviewable state.  Auto-merge
        is NOT requested or enabled — the merge gate stays human.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

        Args:
            ticket_id: The blocked ticket this PR addresses.
            repo_full_name: GitHub ``owner/name``.
            branch_name: The head branch to merge from (must already exist).
            title: PR title. Should reference the ticket id for traceability.
            body: PR description.  Defaults to a message linking back to the
                originating ticket.

        Returns:
            A status message with the PR URL on success, or an error message.

        """
        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        pr_body = body or (
            f"PR opened by robotsix-chat agent to resolve blocked ticket "
            f"`{ticket_id}`.\n\n"
            f"**Auto-merge is disabled** — human review required before merge."
        )
        return await client.create_pr(
            repo_full_name=repo_full_name,
            head_branch=branch_name,
            title=title,
            body=pr_body,
        )

    async def open_simple_repo_pr(
        repo_full_name: str,
        branch_name: str,
        files_json: str,
        title: str,
        body: str = "",
        commit_message: str = "",
    ) -> str:
        """Open a PR for a simple change — NO mill ticket required.

        **Lightweight ungated direct-PR path.**  Creates a branch, commits
        the given files, and opens a reviewable pull request in one call,
        *without* requiring a mill ticket in BLOCKED state.  The opened PR is
        the review gate: a human approves and merges it, so this path stays
        safe and reversible.

        **Use this ONLY for low-risk, simple tasks:**
        - content / text edits (e.g. adding a project card to a website),
        - small documentation changes,
        - single-file or few-file tweaks with no behavioural risk.

        For larger or complex work — anything needing multi-step design,
        broad refactors, or careful review — file a mill ticket and use the
        ticket-gated flow (``push_direct_repo_branch`` + ``open_direct_repo_pr``)
        instead.

        **Not eligible on this path (refused by the tool):** CI workflow /
        composite-action files (``.github/workflows/``, ``.github/actions/``)
        and credential/secret-shaped files (private keys, ``.env`` files).
        Route those — and any destructive change, force-merge, or merge
        operation (still confirmation-gated) — through the ticket-gated flow.

        **Scope:** When called through the component roster the GitHub App
        installation scope check is bypassed.  For direct board-API calls,
        *repo_full_name* must be within the robotsix-mill GitHub App's current
        installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-website"``).
            branch_name: Name for the new branch (e.g.
                ``"chore/add-thalamus-card"``).
            files_json: JSON array of file entries to create or overwrite.
                Same forms as ``push_direct_repo_branch``:
                ``{"path": "...", "content": "..."}`` for text,
                ``{"path": "...", "content_b64": "..."}`` for base64 bytes, or
                ``{"path": "...", "local_path": "..."}`` for a file inside the
                file-hub work directory.
            title: PR title.  Should be a conventional-commit subject
                (``feat:``/``fix:``/``docs:``/``chore:`` …).
            body: PR description.  Defaults to a note describing the
                lightweight direct-PR path and that review is required.
            commit_message: Commit message.  Defaults to *title*.

        Returns:
            A status message with the branch URL and PR URL on success, or an
            error message describing why the change was refused or failed.

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

        # --- guard: reject workflow/secret files on the ungated path ---
        if safety_error := check_simple_pr_file_safety(files):
            return safety_error

        # --- scope check only (NO BLOCKED-state / ticket gate) ---
        if error := await assert_in_scope(client, repo_full_name):
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

        commit_msg = commit_message or title
        push_result = await client.push_branch(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            files=files,
            commit_message=commit_msg,
            ticket_id="simple-pr",
        )
        if push_result.startswith("Error"):
            return push_result

        pr_body = body or (
            "PR opened by robotsix-chat agent via the lightweight "
            "direct-PR path for a simple change.\n\n"
            "**Auto-merge is disabled** — human review required before merge."
        )
        pr_result = await client.create_pr(
            repo_full_name=repo_full_name,
            head_branch=branch_name,
            title=title,
            body=pr_body,
        )
        return f"{push_result}\n\n{pr_result}"

    async def update_simple_repo_pr(
        repo_full_name: str,
        files_json: str,
        pr_number: int = 0,
        branch_name: str = "",
        commit_message: str = "",
    ) -> str:
        """Commit a multi-file changeset to an open PR's branch — NO ticket.

        **Lightweight ungated companion to ``open_simple_repo_pr``.**  Adds a
        commit to an EXISTING open pull request's head branch in one call,
        updating the PR in place (no close/reopen), *without* requiring a mill
        ticket in BLOCKED state.  The open PR is itself the human review gate,
        so this path stays safe and reversible.  Use it to iterate on a PR you
        opened with ``open_simple_repo_pr`` — including a directory/module
        rename, expressed as (delete old paths + create new paths) in one
        commit.  Merging stays confirmation-gated via ``merge_direct_repo_pr``.

        **Not eligible on this path (refused by the tool):** CI workflow /
        composite-action files (``.github/workflows/``, ``.github/actions/``)
        and credential/secret-shaped files (private keys, ``.env`` files) —
        the guard applies to both added AND deleted paths.

        **Scope:** When called through the component roster the GitHub App
        installation scope check is bypassed.  For direct board-API calls,
        *repo_full_name* must be within the robotsix-mill GitHub App's current
        installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-website"``).
            files_json: JSON array of changeset entries.  Reuses the
                ``open_simple_repo_pr`` content forms —
                ``{"path": "...", "content": "..."}`` for text,
                ``{"path": "...", "content_b64": "..."}`` for base64 bytes,
                ``{"path": "...", "local_path": "..."}`` for a file inside the
                file-hub work directory — plus a delete form
                ``{"path": "...", "delete": true}`` to remove a path.  A
                rename is (delete old path + create new path).
            pr_number: The open PR number to update.  Provide EITHER this or
                *branch_name*.
            branch_name: The head branch name of an open PR to update.
                Provide EITHER this or *pr_number*.
            commit_message: Commit message.  Defaults to a message naming the
                target branch.

        Returns:
            A status message with the commit SHA on success, or an error
            message describing why the change was refused or failed.

        """
        try:
            files: list[dict[str, str]] = json.loads(files_json)
        except json.JSONDecodeError, TypeError:
            return (
                "Error: files_json must be a valid JSON array of changeset "
                f"entries — {FILES_JSON_CHANGESET_FORMS}."
            )

        if not isinstance(files, list):
            return "Error: files_json must be a JSON array."

        if entry_error := validate_changeset_entries(files):
            return entry_error

        # --- guard: reject workflow/secret files (added OR deleted) ---
        if safety_error := check_simple_pr_file_safety(files):
            return safety_error

        if not pr_number and not branch_name:
            return (
                "Error: provide either 'pr_number' or 'branch_name' of an open "
                "PR to update."
            )

        # --- scope check only (NO BLOCKED-state / ticket gate) ---
        if error := await assert_in_scope(client, repo_full_name):
            return error

        # --- resolve the target head branch from the PR number or branch ---
        head_branch: str
        if pr_number:
            try:
                pr = await client.get_pr(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                )
            except Exception as exc:
                return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"
            state = pr.get("state", "unknown")
            if state != "open":
                return (
                    f"Error: PR #{pr_number} in {repo_full_name} is {state}, "
                    f"not open — cannot update a closed PR."
                )
            head_info = pr.get("head", {})
            resolved_branch = head_info.get("ref")
            head_repo_full_name = head_info.get("repo", {}).get("full_name")
            if not resolved_branch:
                return (
                    f"Error: PR #{pr_number} in {repo_full_name} has no head "
                    f"branch — cannot determine where to push."
                )
            if head_repo_full_name and head_repo_full_name != repo_full_name:
                return (
                    f"Refused: PR #{pr_number} head branch '{resolved_branch}' "
                    f"belongs to '{head_repo_full_name}', not "
                    f"'{repo_full_name}'. Cross-repo PR updates are not "
                    f"permitted."
                )
            head_branch = resolved_branch
        else:
            try:
                pr = await client.find_open_pr_for_branch(
                    repo_full_name=repo_full_name,
                    branch_name=branch_name,
                )
            except Exception as exc:
                return (
                    f"Error looking up open PR for branch '{branch_name}' in "
                    f"{repo_full_name}: {exc}"
                )
            if not pr:
                return (
                    f"Error: no open PR found with head branch '{branch_name}' "
                    f"in {repo_full_name}."
                )
            head_branch = branch_name

        # --- split the changeset into content writes and deletions ---
        changes = [f for f in files if not f.get("delete")]
        deletes = [str(f.get("path")) for f in files if f.get("delete")]

        msg = commit_message or f"chore: update PR on branch '{head_branch}'"
        return await client.push_files_to_branch(
            repo_full_name=repo_full_name,
            branch_name=head_branch,
            files=changes,
            deletes=deletes,
            commit_message=msg,
        )

    async def update_pr_branch(
        ticket_id: str,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Attempt to rebase a PR branch onto the latest base branch.

        Calls GitHub's update-branch API, which tries to rebase the PR's head
        branch onto the current tip of the base branch.  If the rebase succeeds,
        the PR is updated.  If merge conflicts are detected, the tool returns
        the conflict details so the agent can decide next steps.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

        Args:
            ticket_id: The blocked ticket the PR belongs to (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).
            repo_full_name: GitHub ``owner/name``.
            pr_number: The PR number to update.

        Returns:
            A status message — success with a note that the update is queued,
            or an error describing merge conflicts or other failures.

        """
        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        return await client.update_pr_branch(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )

    async def recover_auto_merge(
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Attempt to recover a PR whose auto-merge has bounced.

        Fetches the PR to verify it is open, then calls GitHub's update-branch
        API to rebase the head branch onto the base.  When auto-merge was
        enabled before the bounce, updating the branch typically re-arms it.

        This tool does **not** require the owning ticket to be in BLOCKED
        state — it is designed for recovery when a green, review-approved PR
        has fallen behind the base branch and auto-merge has failed.

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name``.
            pr_number: The PR number to recover.

        Returns:
            A status message with mergeable state and update-branch outcome.

        """
        # Installation scope check (skipped when component_request is available)
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        # Verify PR exists and is open
        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        state = pr.get("state", "unknown")
        if state != "open":
            return (
                f"PR #{pr_number} in {repo_full_name} is {state}, not open. "
                "Auto-merge recovery only applies to open PRs."
            )

        mergeable_state = pr.get("mergeable_state", "unknown")
        behind_by: int = pr.get("behind_by", pr.get("commits_behind", 0))

        context_lines = [
            f"PR #{pr_number} in {repo_full_name}: {pr.get('title', '(no title)')}",
            f"Mergeable state: {mergeable_state}",
        ]
        if behind_by:
            context_lines.append(f"Behind base by {behind_by} commit(s)")

        result = await client.update_pr_branch(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )

        return "\n".join(context_lines) + "\n\n" + result

    async def _ci_merge_gate(
        repo_full_name: str, pr_number: int, *, override_ci: bool
    ) -> tuple[str | None, str]:
        """Refuse a merge whose head commit is not all-green on check-runs.

        Returns ``(refusal, override_note)``: *refusal* is the message to
        return instead of merging (``None`` to proceed); *override_note* is
        appended to a successful merge result when ``override_ci`` bypassed
        a non-green verdict.  Draft / conflicting / still-computing / already
        merged PRs are left to ``client.merge_pr``'s own pre-flight so its
        diagnostics stay unchanged.
        """
        try:
            pr = await client.get_pr(repo_full_name=repo_full_name, pr_number=pr_number)
        except Exception as exc:
            return (f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}", "")
        if pr.get("draft") or pr.get("merged") or pr.get("mergeable") is not True:
            return (None, "")
        head_sha = str((pr.get("head") or {}).get("sha") or "")
        try:
            if not head_sha:
                raise RuntimeError("PR data carries no head sha")
            summary = summarize_check_runs(
                await ActionsClient(settings).list_check_runs(repo_full_name, head_sha)
            )
        except Exception as exc:
            verdict = f"UNKNOWN — check-runs could not be read ({exc})"
            green = False
        else:
            verdict = summary.describe()
            green = summary.verdict == "green"
        if green:
            return (None, "")
        if override_ci:
            return (
                None,
                f"\nCI override used on operator instruction — the check-run "
                f"verdict for head {head_sha[:8] or '?'} at merge time was: "
                f"{verdict}",
            )
        return (
            f"Refusing to merge PR #{pr_number} in {repo_full_name}: CI verdict "
            f"for head {head_sha[:8] or '?'} is {verdict}. The GitHub App can "
            f"bypass branch rules, so this tool does not merge a PR whose "
            f"check-runs are not all green. Wait for the checks to finish or "
            f"fix them; if the operator explicitly instructs to merge despite "
            f"this verdict, call again with override_ci=true and say so.",
            "",
        )

    async def merge_direct_repo_pr(
        repo_full_name: str,
        pr_number: int,
        pr_title: str = "",
        head_base_branches: str = "",
        merge_method: str = "squash",
        commit_title: str = "",
        commit_message: str = "",
        override_ci: bool = False,
    ) -> str:
        """Merge a pull request in a GitHub repository.

        **This is a confirmation-gated mutation.**  Before calling this tool
        you MUST obtain explicit operator approval in the conversation.
        State the exact repo, PR number, PR title, and head/base branches
        and wait for the operator to confirm before proceeding.  Never merge
        a PR without the operator's explicit consent in-chat.

        **CI gate (enforced by this tool):** the GitHub App this tool merges
        with can BYPASS branch rules, so GitHub will not stop a red PR.  The
        tool therefore reads EVERY check-run of the PR's head commit and
        refuses to merge unless all of them completed without failure.  A
        refusal names the failed / pending checks; wait for them or fix them.
        Only when the operator has explicitly instructed to merge DESPITE the
        stated verdict, call again with ``override_ci=True`` — the override
        and the verdict it bypassed are echoed into the result.

        **Preconditions (also enforced server-side by GitHub):**
        - PR must be mergeable (no conflicts).
        - PR must not be in draft state.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to merge.
            pr_title: The PR title — optional, echoed back in the result
                for auditability when given.
            head_base_branches: Description of head/base branches
                (e.g. ``"fix/ticket → main"``) — used for the
                confirmation echo.
            merge_method: How to merge — ``"squash"`` (default),
                ``"merge"``, or ``"rebase"``.
            commit_title: Optional merge-commit title (squash/merge).
            commit_message: Optional merge-commit body (squash/merge).
            override_ci: ``True`` ONLY when the operator explicitly asked to
                merge despite a non-green CI verdict reported by this tool.

        Returns:
            A status message with the merge commit SHA on success, or an
            actionable error message (conflicts, CI not green, draft, etc.).

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error

        refusal, override_note = await _ci_merge_gate(
            repo_full_name, pr_number, override_ci=override_ci
        )
        if refusal:
            return refusal

        result = await client.merge_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            merge_method=merge_method,
            commit_title=commit_title or None,
            commit_message=commit_message or None,
        )
        # Include confirmation context in the result for auditability
        if "merged successfully" in result.lower():
            result += _confirmation_echo(pr_title, head_base_branches)
            result += override_note
        return result

    async def close_direct_repo_pr(
        repo_full_name: str,
        pr_number: int,
        pr_title: str = "",
        head_base_branches: str = "",
    ) -> str:
        """Close a pull request without merging it.

        **This is a confirmation-gated mutation.**  Before calling this tool
        you MUST obtain explicit operator approval in the conversation.
        State the exact repo, PR number, PR title, and head/base branches
        and wait for the operator to confirm before proceeding.  Never close
        a PR without the operator's explicit consent in-chat.

        Closing a PR preserves the branch — it can be re-opened or a new
        PR created from it later.  Closing is irreversible without
        re-opening the PR.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to close.
            pr_title: The PR title — optional, echoed back in the result
                for auditability when given.
            head_base_branches: Description of head/base branches
                (e.g. ``"fix/ticket → main"``) — used for the
                confirmation echo.

        Returns:
            A status message confirming the closure, or an actionable error
            message (already closed, already merged, not in scope, etc.).

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error

        result = await client.close_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        # Include confirmation context in the result for auditability
        if "has been closed" in result.lower():
            result += _confirmation_echo(pr_title, head_base_branches)
        return result

    async def check_direct_repo_auto_merge(
        repo_full_name: str,
    ) -> str:
        """Check whether a repository has auto-merge enabled.

        Calls the GitHub API to read the repository's ``allow_auto_merge``
        setting.  Use this **before** filing or managing tickets that
        require automatic merging — if auto-merge is disabled the
        operator should be informed that manual merging will be required.

        **Read-only.**  This tool does not modify any state and does not
        require confirmation gating.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).

        Returns:
            A message indicating whether auto-merge is enabled or disabled,
            or an error message if the repository could not be fetched.

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error
        return await client.check_auto_merge_enabled(
            repo_full_name=repo_full_name,
        )

    async def arm_direct_repo_auto_merge(
        repo_full_name: str,
        pr_number: int,
        pr_title: str = "",
        head_base_branches: str = "",
        merge_method: str = "squash",
    ) -> str:
        """Enable auto-merge on a pull request.

        When auto-merge is enabled GitHub will automatically merge the PR
        as soon as all required conditions are met (CI passes, reviews are
        submitted, branch protection rules are satisfied).  The merge
        happens without further human intervention.

        **This is a confirmation-gated mutation.**  Before calling this tool
        you MUST obtain explicit operator approval in the conversation.
        State the exact repo, PR number, PR title, and head/base branches
        and wait for the operator to confirm before proceeding.  Never
        enable auto-merge without the operator's explicit consent in-chat.

        **Pre-flight:** Call ``check_direct_repo_auto_merge`` first to
        verify the repository has auto-merge enabled.  If auto-merge is
        disabled at the repo level, inform the operator that manual
        merging will be required — ``arm_direct_repo_auto_merge`` will
        fail on a repo with ``allow_auto_merge`` set to false.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to enable auto-merge on.
            pr_title: The PR title — optional, echoed back in the result
                for auditability when given.
            head_base_branches: Description of head/base branches
                (e.g. ``"fix/ticket → main"``) — used for the
                confirmation echo.
            merge_method: Merge strategy to use when auto-merge fires —
                ``"squash"`` (default), ``"merge"``, or ``"rebase"``.

        Returns:
            A success message, or an actionable error message.

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error

        result = await client.arm_auto_merge(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            merge_method=merge_method,
        )
        # Include confirmation context in the result for auditability
        if "auto-merge enabled" in result.lower():
            result += _confirmation_echo(pr_title, head_base_branches)
        return result

    return [
        open_direct_repo_pr,
        open_simple_repo_pr,
        update_simple_repo_pr,
        update_pr_branch,
        recover_auto_merge,
        merge_direct_repo_pr,
        close_direct_repo_pr,
        check_direct_repo_auto_merge,
        arm_direct_repo_auto_merge,
    ]
