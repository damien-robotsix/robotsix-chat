"""GitHub repo/PR operation tools for the direct-repo capability.

Factory :func:`build_github_tools` returns the agent-facing tool closures
that push branches, open/update/merge PRs, check CI status, recover
auto-merge, reset the implement-spawn counter, and apply patches.  It
receives the already-constructed :class:`DirectRepoClient` /
:class:`BoardClient` plus the ticket-precondition helpers from
:mod:`robotsix_chat.repo.direct`, keeping the closures thin and testable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

    from .board_client import BoardClient
    from .client import DirectRepoClient

logger = logging.getLogger(__name__)


def build_github_tools(
    *,
    client: DirectRepoClient,
    board: BoardClient,
    settings: DirectRepoSettings,
    component_request: Callable[..., Any] | None,
    assert_blocked_and_scoped: Callable[..., Awaitable[str | None]],
    assert_in_scope: Callable[..., Awaitable[str | None]],
) -> list[Callable[..., Any]]:
    """Build the GitHub repo/PR operation tools.

    The returned closures capture *client* / *board* / *settings* /
    *component_request* and the two precondition helpers supplied by the
    caller (``_assert_blocked_and_scoped`` and ``_assert_in_scope`` from
    :mod:`robotsix_chat.repo.direct`).
    """
    from robotsix_chat.common.unified_diff import apply_patch as _apply_patch

    from .actions_client import ActionsClient

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
            files_json: JSON array of ``{"path": "...", "content": "..."}``
                objects describing the files to create or overwrite.
                Paths are relative to the repo root.
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
                "Error: files_json must be a valid JSON array "
                "of {path, content} objects."
            )

        if not isinstance(files, list):
            return "Error: files_json must be a JSON array."

        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        # --- ensure changelog fragments end with a newline ---
        for f in files:
            if (
                f.get("path", "").startswith("changelog.d/")
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

    async def check_pr_merge_conflict(
        ticket_id: str,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Check whether a PR has merge conflicts.

        Fetches the PR's mergeability status from GitHub and returns a
        human-readable summary including the mergeable state and, when
        available, the specific conflict reason.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** When called through the component roster (i.e. the
        ``component_request`` credential is available) the GitHub App
        installation scope check is bypassed — the mill already has its
        own GitHub access.  For direct board-API calls, *repo_full_name*
        must be within the robotsix-mill GitHub App's current installation
        scope (checked dynamically at call time).

        Args:
            ticket_id: The blocked ticket the PR belongs to.
            repo_full_name: GitHub ``owner/name``.
            pr_number: The PR number to inspect.

        Returns:
            A status message with mergeability details, or an error message.

        """
        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number}: {exc}"

        mergeable = pr.get("mergeable")
        mergeable_state = pr.get("mergeable_state", "unknown")
        title = pr.get("title", "(no title)")
        html_url = pr.get("html_url", "")

        lines = [
            f"PR #{pr_number} in {repo_full_name}: {title}",
            f"URL: {html_url}",
            f"Mergeable state: {mergeable_state}",
        ]

        if mergeable is None:
            lines.append(
                "Mergeability is still being computed by GitHub — "
                "try again in a few seconds."
            )
        elif mergeable is True:
            lines.append("No merge conflicts detected — PR is mergeable.")
        elif mergeable is False:
            lines.append(
                "Merge conflicts detected — the PR cannot be merged as-is. "
                "Consider rebasing the branch or resolving conflicts manually."
            )

        # Include additional fields that may carry useful conflict info
        for field in ("merged", "merged_at", "merge_commit_sha", "draft"):
            val = pr.get(field)
            if val is not None:
                lines.append(f"{field}: {val}")

        return "\n".join(lines)

    async def verify_pr_ci_status(
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Fetch live CI run status and PR state from GitHub.

        Combines PR metadata (state, mergeability, draft status) with the
        latest CI workflow runs for the PR's head branch into a single
        human-readable summary.  Use this tool BEFORE asserting success or
        signalling the operator about CI/PR status — never rely on cached
        or inferred data.

        **Read-only.** Does not modify any repository state.
        **No BLOCKED-state requirement.** This is a pure diagnostic tool —
        it does not require a ticket to be in BLOCKED state.

        When GitHub is unreachable the tool returns an explicit error
        message rather than guessing.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to inspect.

        Returns:
            A multi-line summary: PR state, mergeability, draft status,
            and the latest CI workflow runs for the PR's head branch.

        """
        # Scope check (no BLOCKED-state requirement — this is read-only)
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        title = pr.get("title", "(no title)")
        state = pr.get("state", "unknown")
        html_url = pr.get("html_url", "")
        mergeable = pr.get("mergeable")
        mergeable_state = pr.get("mergeable_state", "unknown")
        draft = pr.get("draft", False)
        merged = pr.get("merged", False)
        head = pr.get("head", {})
        head_branch = head.get("ref", "")

        lines = [
            f"PR #{pr_number} in {repo_full_name}: {title}",
            f"URL: {html_url}",
            f"State: {state}",
            f"Draft: {draft}",
            f"Merged: {merged}",
            f"Mergeable state: {mergeable_state}",
        ]

        if mergeable is None:
            lines.append("Mergeability: still being computed by GitHub.")
        elif mergeable is True:
            lines.append("Mergeability: clean — no conflicts.")
        elif mergeable is False:
            lines.append("Mergeability: conflicts detected.")

        # --- CI workflow runs for the PR's head branch ---
        if head_branch:
            try:
                actions_client = ActionsClient(settings)
                runs = await actions_client.list_workflow_runs(
                    repo_full_name, branch=head_branch, per_page=5
                )
            except Exception as exc:
                lines.append(f"CI status: could not fetch workflow runs — {exc}")
                return "\n".join(lines)

            if not runs:
                lines.append(
                    f"CI status: no recent workflow runs found "
                    f"for branch '{head_branch}'."
                )
            else:
                lines.append(
                    f"CI status for branch '{head_branch}' ({len(runs)} recent run(s)):"
                )
                for r in runs[:5]:
                    lines.append(
                        f"  - {r.get('name', '?')} "
                        f"(run {r.get('id')}): "
                        f"status={r.get('status')}, "
                        f"conclusion={r.get('conclusion')}"
                    )
        else:
            lines.append("CI status: could not determine head branch from PR data.")

        return "\n".join(lines)

    async def check_ci_health(
        repo_full_name: str,
        branch: str = "",
    ) -> str:
        """Check recent CI history for a repository branch and classify failures.

        Lists the most recent workflow runs on *branch* (default: the
        repository's default branch) and compares the latest run against the
        most recent green run.  Use this BEFORE asserting that a CI failure is
        pre-existing — never rely on cached or inferred status.  This is the
        first step when a deployment is blocked because a dependent PR cannot
        merge due to CI failures on the base branch.

        **Read-only.** Does not modify any state and does not require a ticket
        to be in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            branch: Branch to inspect.  Defaults to the repository's default
                branch when empty.

        Returns:
            A multi-line summary of recent runs plus a verdict: whether the
            latest failure is pre-existing (an earlier recent run was green)
            and whether a rerun or escalation is recommended.

        """
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        actions_client = ActionsClient(settings)
        target_branch = branch or await actions_client.get_default_branch(
            repo_full_name
        )

        try:
            runs = await actions_client.list_workflow_runs(
                repo_full_name,
                branch=target_branch,
                per_page=20,
                raise_on_error=True,
            )
        except Exception as exc:
            return f"Error checking CI health for {repo_full_name}: {exc}"

        lines = [f"CI health for {repo_full_name} branch '{target_branch}':"]
        if not runs:
            lines.append("No recent workflow runs found.")
            lines.append(
                "Recommendation: escalate — there is no CI history available "
                "to compare against."
            )
            return "\n".join(lines)

        lines.append(f"{len(runs)} recent run(s):")
        for r in runs:
            lines.append(
                f"  - {r.get('name', '?')} (run {r.get('id')}): "
                f"status={r.get('status')}, conclusion={r.get('conclusion')}"
            )

        latest = runs[0]
        latest_conclusion = (latest.get("conclusion") or "").lower()
        recent_green = next(
            (r for r in runs if (r.get("conclusion") or "").lower() == "success"),
            None,
        )

        failing = {"failure", "cancelled", "timed_out", "startup_failure"}
        if latest_conclusion in failing and recent_green is not None:
            lines.append(
                "Verdict: PRE-EXISTING failure — the latest run is failing but "
                f"an earlier recent run ({recent_green.get('name', '?')} run "
                f"{recent_green.get('id')}) was green, so branch "
                f"'{target_branch}' is red independently of any dependent PR."
            )
            lines.append(
                "Recommendation: rerun the failing run or file a CI "
                "stabilization ticket."
            )
        elif latest_conclusion == "success":
            lines.append("Verdict: GREEN — the latest run on this branch succeeded.")
        elif latest_conclusion in failing:
            lines.append(
                "Verdict: FAILING but no green run in the recent window — "
                "cannot confirm the failure is pre-existing from this history "
                "alone; widen the window or inspect the failing job logs."
            )
        else:
            lines.append(
                f"Verdict: latest run is '{latest.get('status')}' "
                f"(conclusion='{latest.get('conclusion')}') — no final verdict yet."
            )

        return "\n".join(lines)

    async def rerun_ci_workflow(
        repo_full_name: str,
        branch: str = "",
        run_id: int = 0,
    ) -> str:
        """Re-run a failed CI workflow run on a repository branch.

        When *run_id* is provided it re-runs that specific run; otherwise it
        re-runs the most recent failed run on *branch* (default: the
        repository's default branch).  Use this after ``check_ci_health``
        confirms a CI failure when a deployment is blocked by a dependent PR
        that cannot merge.

        **This is a confirmation-gated mutation.** Only re-run after the
        operator has explicitly consented in the conversation.  The endpoint
        triggers a new CI run (consuming Actions minutes); it does not modify
        repository source.

        **No BLOCKED-state requirement.** This is a follow-up remediation
        operation, like merge/auto-merge — it does not require a ticket to be
        in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name``.
            branch: Branch whose latest failed run should be re-run when
                *run_id* is not supplied.
            run_id: Specific workflow run id to re-run (optional).

        Returns:
            A status message, or an error describing why the re-run failed.

        """
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        actions_client = ActionsClient(settings)
        if not run_id:
            target_branch = branch or await actions_client.get_default_branch(
                repo_full_name
            )
            try:
                runs = await actions_client.list_workflow_runs(
                    repo_full_name,
                    branch=target_branch,
                    per_page=20,
                    raise_on_error=True,
                )
            except Exception as exc:
                return f"Error listing workflow runs for {repo_full_name}: {exc}"
            failed = next(
                (
                    r
                    for r in runs
                    if (r.get("conclusion") or "").lower()
                    in {"failure", "cancelled", "timed_out", "startup_failure"}
                ),
                None,
            )
            if failed is None:
                return (
                    f"No failed workflow run found on '{target_branch}' in "
                    f"{repo_full_name} — nothing to re-run."
                )
            resolved_id = failed.get("id")
            if not isinstance(resolved_id, int):
                return "Error: could not determine the failed run id."
            run_id = resolved_id

        return await actions_client.rerun_workflow_run(repo_full_name, run_id)

    async def file_ci_stabilization_ticket(
        repo_full_name: str,
        branch: str = "",
        summary: str = "",
    ) -> str:
        """File a dedicated CI-stabilization ticket on the board.

        Escalates a CI failure to a human operator by creating a board ticket
        that flags the repository/branch for CI stabilization.  Use this when
        a deployment is blocked because a dependent PR cannot merge due to
        pre-existing CI failures and a simple re-run is not appropriate or
        has not resolved the problem.

        **No BLOCKED-state requirement.** Filing a ticket is an escalation
        action and does not require a ticket to already be in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name`` with the failing CI.
            branch: Branch with the failing CI (default: repository default).
            summary: Short description of the failure to include in the ticket
                body (optional).

        Returns:
            The created ticket id and title, or an error message.

        """
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        actions_client = ActionsClient(settings)
        target_branch = branch or await actions_client.get_default_branch(
            repo_full_name
        )

        title = f"CI stabilization needed: {repo_full_name} ({target_branch})"
        body_lines = [
            f"CI on `{repo_full_name}` branch `{target_branch}` is failing and "
            f"may be blocking dependent PRs from merging.",
        ]
        if summary:
            body_lines.append("")
            body_lines.append(f"Summary: {summary}")
        body_lines.append("")
        body_lines.append(
            "Filed by the robotsix-chat agent as a CI-stabilization "
            "escalation. Verify the failure, re-run CI if it looks transient, "
            "and remediate the root cause."
        )

        try:
            ticket_id = await board.create_ticket(
                title=title,
                description="\n".join(body_lines),
                kind="task",
                source="agent",
            )
        except Exception as exc:
            return f"Error filing CI stabilization ticket for {repo_full_name}: {exc}"

        if not ticket_id:
            return (
                f"Error filing CI stabilization ticket for {repo_full_name}: "
                f"board API did not return a ticket id."
            )
        return f"Filed CI stabilization ticket {ticket_id}: {title}"

    async def list_open_prs(
        org_name: str,
    ) -> str:
        """List open pull requests across an organization's repositories in one batch.

        Uses GitHub's Search API (``/search/issues?q=type:pr+state:open+org:...``)
        to return every open PR the GitHub App can access in *org_name* with a
        single search query — instead of one WebFetch/API call per repository.
        Prefer this tool whenever the user asks about PRs across several
        repositories (a batch-worthy number of targets).

        **Read-only.** Does not modify any repository state and does not
        require a ticket to be in BLOCKED state.  Results are limited to the
        repositories the robotsix-mill GitHub App is installed on.

        Args:
            org_name: GitHub organization name (e.g. ``"robotsix"``).

        Returns:
            A summary grouped by repository listing each open PR's number,
            title, URL, author, and draft status — plus a total count and a
            truncation note when GitHub's search-result limit is reached.

        """
        try:
            items = await client.search_open_prs(org_name=org_name)
        except Exception as exc:
            return f"Error listing open PRs for org '{org_name}': {exc}"

        if not items:
            return (
                f"No open PRs found for org '{org_name}' "
                "(in repositories the GitHub App can access)."
            )

        by_repo: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            repository_url = item.get("repository_url", "")
            repo = (
                repository_url.rsplit("/repos/", 1)[-1]
                if "/repos/" in repository_url
                else "(unknown repo)"
            )
            by_repo.setdefault(repo, []).append(item)

        lines = [
            f"Open PRs across org '{org_name}' — {len(items)} total:",
        ]
        for repo in sorted(by_repo):
            lines.append(f"\n{repo}:")
            for item in sorted(by_repo[repo], key=lambda p: p.get("number", 0)):
                number = item.get("number", "?")
                title = item.get("title", "(no title)")
                html_url = item.get("html_url", "")
                author = (item.get("user") or {}).get("login", "unknown")
                draft = " [draft]" if item.get("draft") else ""
                lines.append(f"  - #{number} {title}{draft} (by {author}) — {html_url}")

        if len(items) >= 1000:
            lines.append(
                "\nNote: GitHub's search API caps results at 1000 items; "
                "there may be more open PRs than shown."
            )

        return "\n".join(lines)

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

    async def merge_direct_repo_pr(
        repo_full_name: str,
        pr_number: int,
        pr_title: str,
        head_base_branches: str,
        merge_method: str = "squash",
        commit_title: str = "",
        commit_message: str = "",
    ) -> str:
        """Merge a pull request in a GitHub repository.

        **This is a confirmation-gated mutation.**  Before calling this tool
        you MUST obtain explicit operator approval in the conversation.
        State the exact repo, PR number, PR title, and head/base branches
        and wait for the operator to confirm before proceeding.  Never merge
        a PR without the operator's explicit consent in-chat.

        **Preconditions (enforced server-side by GitHub):**
        - PR must be mergeable (no conflicts).
        - Required status checks / CI must be green.
        - PR must not be in draft state.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to merge.
            pr_title: The PR title — used for the confirmation echo.
            head_base_branches: Description of head/base branches
                (e.g. ``"fix/ticket → main"``) — used for the
                confirmation echo.
            merge_method: How to merge — ``"squash"`` (default),
                ``"merge"``, or ``"rebase"``.
            commit_title: Optional merge-commit title (squash/merge).
            commit_message: Optional merge-commit body (squash/merge).

        Returns:
            A status message with the merge commit SHA on success, or an
            actionable error message (conflicts, CI not green, draft, etc.).

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error

        result = await client.merge_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            merge_method=merge_method,
            commit_title=commit_title or None,
            commit_message=commit_message or None,
        )
        # Include confirmation context in the result for auditability
        if "merged successfully" in result.lower():
            result += f"\nPR: {pr_title}\nBranches: {head_base_branches}"
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
        pr_title: str,
        head_base_branches: str,
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
            pr_title: The PR title — used for the confirmation echo.
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
            result += f"\nPR: {pr_title}\nBranches: {head_base_branches}"
        return result

    async def enable_repo_pages(
        repo_full_name: str,
        build_type: str = "workflow",
    ) -> str:
        """Enable GitHub Pages built from a workflow on a repository.

        Enables Pages via ``POST /repos/{owner}/{repo}/pages`` with
        ``build_type: workflow`` and reads the resulting site status back.
        Idempotent — a repo that already has Pages returns an
        already-enabled result instead of an error.

        **This is a confirmation-gated mutation.**  This changes live
        repository settings.  Before calling this tool you MUST obtain
        explicit operator approval in the conversation (the same class of
        mutation as ``set_repo_security_and_analysis``).  State the exact
        repository and wait for the operator to confirm before proceeding.

        **No BLOCKED-state requirement.** This is a confirmation-gated
        tool — it does not require a ticket to be in BLOCKED state.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            build_type: Pages build type — ``"workflow"`` (default, built
                via GitHub Actions) or ``"legacy"``.

        Returns:
            A message reporting the enable result and the resulting Pages
            site status, or an actionable error (e.g. permission denied).

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error
        return await client.enable_pages(
            repo_full_name=repo_full_name,
            build_type=build_type,
        )

    async def reset_implement_spawn_counter(ticket_id: str) -> str:
        """Reset the implement-agent spawn counter for a blocked ticket.

        Sends ``POST /tickets/{ticket_id}/resume-blocked`` to the board API
        with a spawn-counter justification, clearing the spawn-limit block
        so the implement agent can be re-spawned against this ticket.

        Tries the component roster path first when *component_request*
        is available (resolving ``"mill"`` via the central-deploy roster
        or component fallbacks); on failure falls back to the direct
        ``board_api_base_url`` path.  When *component_request* is
        unavailable the direct path is used directly.

        On board builds that do not expose the ``implement_spawn_count``
        artifact DELETE route the call returns **HTTP 405 / an error**.
        Do not retry the reset — use the ``resume-blocked`` mechanism
        (``POST /tickets/{id}/resume-blocked``) as the standard fallback.

        Args:
            ticket_id: The blocked ticket whose counter to reset
                (e.g. ``"20250624T020652Z-my-ticket-a1b2"``).

        Returns:
            A status message — success confirmation, or a
            ``MANUAL INTERVENTION REQUIRED`` error describing the board
            API failure, the ticket's current visibility on the board, and
            the next step for a human operator.

        """
        # Resolve paraphrased / abbreviated IDs before making the request.
        resolved_map = await board.resolve_ticket_ids([ticket_id])
        effective_id = resolved_map.get(ticket_id) or ticket_id

        justification = (
            "Spawn counter reset — allowing re-implement after spawn limit reached."
        )

        # Try roster path first when available.
        if component_request is not None:
            resp = await component_request(
                "mill",
                "POST",
                f"/tickets/{effective_id}/resume-blocked",
                json_body={"justification": justification},
            )
            if resp.startswith("HTTP 2"):
                return (
                    f"Implement spawn counter reset for ticket {effective_id} "
                    "(via roster path). The ticket can now be re-spawned."
                )
            logger.info(
                "reset_implement_spawn_counter roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        # Fall back to the direct board API path.
        ok, reason = await board.resume_blocked_ticket(effective_id, justification)
        if ok:
            return (
                f"Implement spawn counter reset for ticket {effective_id}. "
                "The ticket can now be re-spawned."
            )

        board_url = board._board_url
        # Even when resume fails, confirm whether the ticket is still visible
        # on the board so the operator knows if this is a connectivity/API
        # failure or a ticket that has vanished from the board.
        state = await board.get_ticket_state(effective_id)
        if state:
            visibility = (
                f"Ticket {effective_id} is still visible on the board "
                f"(current state: {state})."
            )
        else:
            visibility = (
                f"Ticket {effective_id} could not be located on the board — "
                "it may have been deleted or the board API is unreachable."
            )
        return (
            f"Error: could not reset implement spawn counter for ticket "
            f"{effective_id}.\n"
            "MANUAL INTERVENTION REQUIRED: resume-blocked failed with "
            f"{reason or 'unknown board API error'}.\n"
            f"{visibility}\n"
            f"Board URL: {board_url}. A human should check the ticket on the "
            "board and resume it manually."
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
        file_path: str,
        patch_content: str,
        commit_message: str = "",
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
            patch_content: The unified diff to apply.  Must include at
                least one ``@@`` hunk header with context lines.
            commit_message: Commit message.  Defaults to a message that
                references the *ticket_id*.

        Returns:
            A status message with the commit SHA on success, or an error
            message describing why the push was refused or failed.

        """
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

    async def inspect_github_installation_token(
        repo_full_name: str,
    ) -> str:
        """Inspect the GitHub App installation token's expiry and permission scope.

        Mints a **fresh** installation token for *repo_full_name* (bypassing any
        cached token) so the returned permission map reflects the App's current
        grant — not a possibly-stale cached token.  Use this to distinguish
        "the token was cached/stale" from "the App genuinely lacks a
        permission" when a GitHub API call fails with a 403 permission error
        such as ``lacks pages: write``.

        **Read-only.** Does not modify any repository state and does not
        require a ticket to be in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g. ``"robotsix/robotsix-chat"``).

        Returns:
            A report with the App id, configured and resolved installation ids,
            token expiry timestamp, seconds remaining, and the token's
            effective permission map.  A mismatch between the configured and
            resolved installation ids is called out explicitly.

        """
        try:
            details = await client.get_installation_token_diagnostics(repo_full_name)
        except Exception as exc:
            return f"Error inspecting installation token for {repo_full_name}: {exc}"

        permissions: dict[str, str] = details["permissions"]
        configured_id = details["configured_installation_id"]
        resolved_id = details["resolved_installation_id"]
        lines = [
            f"GitHub App installation token diagnostic for `{repo_full_name}`:",
            f"- App id: `{details['app_id']}`",
            f"- Configured installation id: `{configured_id}`",
            f"- Resolved installation id (for `{repo_full_name}`): `{resolved_id}`",
            f"- Token expires at: `{details['expires_at']}` (UTC)",
            f"- Seconds remaining: {details['seconds_remaining']}",
        ]
        if resolved_id != configured_id:
            lines.append(
                "  ⚠️ Mismatch: the resolved installation id differs from the "
                "configured id — this repo is installed under a different "
                "installation of the App than the one in config. The permission "
                "map below reflects the resolved installation."
            )
        lines.append("- Permissions (effective scope):")
        if not permissions:
            lines.append(
                "  - (none returned — the installation may have no permissions granted)"
            )
        else:
            for name in sorted(permissions):
                lines.append(f"  - `{name}`: `{permissions[name]}`")
        return "\n".join(lines)

    return [
        push_direct_repo_branch,
        open_direct_repo_pr,
        update_pr_branch,
        check_pr_merge_conflict,
        verify_pr_ci_status,
        check_ci_health,
        rerun_ci_workflow,
        file_ci_stabilization_ticket,
        recover_auto_merge,
        check_direct_repo_auto_merge,
        list_open_prs,
        merge_direct_repo_pr,
        arm_direct_repo_auto_merge,
        enable_repo_pages,
        reset_implement_spawn_counter,
        apply_patch_to_file,
        push_patch_to_pr_branch,
        inspect_github_installation_token,
    ]
