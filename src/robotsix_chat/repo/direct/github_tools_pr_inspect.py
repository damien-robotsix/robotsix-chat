"""PR inspection and merge-conflict tools for :func:`build_github_tools`, split out.

The tool closures below were extracted verbatim from
:func:`robotsix_chat.repo.direct.github_tools.build_github_tools` to keep that
factory manageable.  ``build_github_tools`` calls
:func:`build_github_tools_pr_inspect` and splices the returned tools back into its
result list at their original positions, so the public tool set (names,
count, order) is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .github_tools import resolve_pr_ref

if TYPE_CHECKING:
    from .board_client import BoardClient
    from .client import DirectRepoClient


def build_github_tools_pr_inspect(
    *,
    client: DirectRepoClient,
    board: BoardClient,
    component_request: Callable[..., Any] | None,
    assert_blocked_and_scoped: Callable[..., Awaitable[str | None]],
) -> list[Callable[..., Any]]:
    """Build and return the 4 PR inspection and merge-conflict tools.

    Returned in definition order; ``build_github_tools`` unpacks them
    positionally.  The closures capture only this function's parameters.
    """

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

    async def inspect_pr_diff(
        repo_full_name: str | None = None,
        pr_number: int | None = None,
        repo: str | None = None,
        pr_url: str | None = None,
        ticket_id: str | None = None,
    ) -> str:
        """Fetch the raw unified diff of an open pull request.

        Returns the full diff of a PR — every file changed, every line added
        or removed — as a unified diff.  Use this tool BEFORE merging a PR to
        verify that the diff actually delivers what the ticket requires (e.g.
        whether a CI migration PR actually adopts shared workflows vs. reverting
        to inline jobs).  The diff is returned as plain text; inspect it for the
        patterns the ticket's acceptance criteria require.

        **Read-only.** Does not modify any repository state.
        **No BLOCKED-state requirement.** This is a pure diagnostic tool —
        it does not require a ticket to be in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``). ``repo`` is an accepted
                alias; a bare repo name is resolved via the board roster.
            pr_number: The PR number to inspect.
            repo: Alias of ``repo_full_name``.
            pr_url: Alternative to repo + number — the PR's GitHub url.
            ticket_id: Alternative — a mill ticket id; its open PR is used.

        Returns:
            The raw unified diff of the PR as a string.  Large diffs may be
            truncated — the tool prepends a line count and truncation note
            when the diff exceeds 8000 characters.

        """
        ref = await resolve_pr_ref(
            board,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            repo=repo,
            pr_url=pr_url,
            ticket_id=ticket_id,
        )
        if isinstance(ref, str):
            return ref
        repo_full_name, pr_number = ref
        # Scope check (no BLOCKED-state requirement — this is read-only)
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        try:
            diff_text = await client.get_pr_diff(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching diff for PR #{pr_number} in {repo_full_name}: {exc}"

        if not diff_text:
            return (
                f"PR #{pr_number} in {repo_full_name}: diff is empty (no file changes)."
            )

        max_chars = 8000
        line_count = diff_text.count("\n") + (1 if diff_text else 0)
        preamble = (
            f"PR #{pr_number} diff ({line_count} lines, "
            f"{len(diff_text)} chars total):\n\n"
        )

        if len(diff_text) <= max_chars:
            return preamble + diff_text

        truncated = diff_text[:max_chars]
        return (
            f"{preamble}"
            f"{truncated}\n\n"
            f"... [truncated: {len(diff_text) - max_chars} more chars, "
            f"{diff_text.count('\n', max_chars)} more lines — "
            f"review the full diff at the PR URL]"
        )

    async def list_open_prs(
        repo: str = "",
        owner: str = "",
        state: str = "all",
        since_days: int = 30,
        repo_full_name: str = "",
    ) -> str:
        """List pull requests for a repository or the whole GitHub account in one batch.

        Uses GitHub's Search API (``/search/issues?q=type:pr …``) to return
        every matching PR the GitHub App can access with a single query —
        instead of one API call per repository.  Despite the name it
        lists *closed and merged* PRs too (``state="all"`` is the default
        with a ``since_days`` window), so a merged PR is visible instead
        of vanishing into "No open PRs".

        **Repository identity.** *repo* may be a mill ``repo_id`` (e.g.
        ``"robotsix-central-deploy"``) — it is resolved to ``owner/repo``
        through the mill's repo registry — or a full ``owner/repo``.  When
        *repo* is empty the search spans the account the GitHub App is
        installed on (*owner*, defaulting to the installation's own
        account).  Never pass a guessed organisation name.

        **Read-only.** Does not modify any repository state and does not
        require a ticket to be in ``blocked`` state.

        Args:
            repo: Mill ``repo_id`` or ``owner/repo``; empty for account-wide.
            owner: GitHub account to search when *repo* is empty.  Defaults
                to the GitHub App installation's account.
            state: ``"open"``, ``"closed"`` or ``"all"`` (default).
            since_days: Only PRs updated within this many days (default 30;
                ``0`` disables the window).  Ignored when *state* is
                ``"open"``.
            repo_full_name: Alias of *repo* (``owner/repo``), accepted so
                this tool takes the same repository argument as every
                other GitHub tool.  Pass either *repo* or
                *repo_full_name*; giving both with different values is an
                error.

        Returns:
            A summary grouped by repository listing each PR's number,
            title, URL, author, state (open / merged / closed) and draft
            status — plus a total count and a truncation note when
            GitHub's search-result limit is reached.

        """
        state_norm = state.strip().lower() or "all"
        if state_norm not in ("open", "closed", "all"):
            return f"Error: state must be 'open', 'closed' or 'all' (got {state!r})."

        # ``repo_full_name`` is the argument name every other GitHub tool
        # uses; accept it here as an alias so the model does not burn a
        # turn on "Additional properties are not allowed" when it guesses.
        repo_arg = repo.strip()
        alias_arg = repo_full_name.strip()
        if repo_arg and alias_arg and repo_arg != alias_arg:
            return (
                f"Error: repo={repo!r} and repo_full_name={repo_full_name!r} "
                "disagree; pass only one of them."
            )
        repo = repo_arg or alias_arg

        resolved_full_name: str | None = None
        scope_owner: str | None = None
        if repo:
            resolved_full_name = await board.resolve_repo_full_name(repo)
            if resolved_full_name is None:
                return (
                    f"Error: {repo!r} is neither a registered mill repo_id nor "
                    "an 'owner/repo' full name.  Call resolve_repo(repo_id) "
                    "to look it up in the mill registry, or pass the exact "
                    "owner/repo — do not guess an organisation."
                )
        else:
            scope_owner = owner.strip() or None
            if scope_owner is None:
                try:
                    scope_owner = await client.installation_account()
                except Exception as exc:
                    return f"Error resolving the GitHub App installation account: {exc}"
                if scope_owner is None:
                    return (
                        "Error: the GitHub App installation has no repositories, "
                        "so there is no account to search; pass owner explicitly."
                    )

        since: str | None = None
        if state_norm != "open" and since_days > 0:
            from datetime import UTC, datetime, timedelta

            since = (datetime.now(UTC) - timedelta(days=since_days)).strftime(
                "%Y-%m-%d"
            )

        scope_label = resolved_full_name or f"account '{scope_owner}'"
        try:
            items = await client.search_prs(
                owner=scope_owner,
                repo_full_name=resolved_full_name,
                state=state_norm,
                since=since,
            )
        except Exception as exc:
            return f"Error listing PRs for {scope_label}: {exc}"

        window = f" updated in the last {since_days} days" if since else ""
        if not items:
            return (
                f"No {state_norm} PRs found for {scope_label}{window} "
                "(in repositories the GitHub App can access)."
            )

        by_repo: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            repository_url = item.get("repository_url", "")
            repo_key = (
                repository_url.rsplit("/repos/", 1)[-1]
                if "/repos/" in repository_url
                else "(unknown repo)"
            )
            by_repo.setdefault(repo_key, []).append(item)

        lines = [
            f"PRs ({state_norm}{window}) for {scope_label} — {len(items)} total:",
        ]
        for repo_key in sorted(by_repo):
            lines.append(f"\n{repo_key}:")
            for item in sorted(by_repo[repo_key], key=lambda p: p.get("number", 0)):
                number = item.get("number", "?")
                title = item.get("title", "(no title)")
                html_url = item.get("html_url", "")
                author = (item.get("user") or {}).get("login", "unknown")
                draft = " [draft]" if item.get("draft") else ""
                pr_meta = item.get("pull_request") or {}
                if item.get("state") == "closed":
                    pr_state = "merged" if pr_meta.get("merged_at") else "closed"
                else:
                    pr_state = "open"
                lines.append(
                    f"  - #{number} {title}{draft} [{pr_state}] "
                    f"(by {author}) — {html_url}"
                )

        if len(items) >= 1000:
            lines.append(
                "\nNote: GitHub's search API caps results at 1000 items; "
                "there may be more PRs than shown."
            )

        return "\n".join(lines)

    async def resolve_pr_conflict(
        ticket_id: str,
        repo_full_name: str,
        pr_number: int,
        resolved_files_json: str,
        commit_message: str = "",
    ) -> str:
        """Resolve a PR's merge conflict by creating a merge commit on its head branch.

        Use this tool when a PR opened for a blocked ticket has developed a
        merge conflict with its base branch (e.g. after ``update_pr_branch``
        returns 422, or ``check_pr_merge_conflict`` reports
        ``mergeable=False``).  Pushing an ordinary commit to the head branch
        does NOT clear a base↔head conflict — the base branch is still not an
        ancestor of the head.  This tool clears the conflict by creating a
        **merge commit** on the head branch whose parents are ``[head SHA,
        base SHA]``.  Once base is an ancestor of head, GitHub recomputes the
        PR as mergeable and the CI-fix chain can proceed.

        The merge commit's tree is the head commit's tree with the resolved
        file contents overlaid on top.  Conflicted paths SHOULD be listed in
        *resolved_files_json* with their merged content; paths that are not
        listed keep their head-branch content, so an empty list resolves
        every conflict in favour of the head branch.

        **Precondition:** The ticket identified by *ticket_id* MUST be in
        BLOCKED state.  This tool will verify that and refuse otherwise.

        **Scope:** When called through the component roster the GitHub App
        installation scope check is bypassed — the mill already has its own
        GitHub access.  For direct board-API calls, *repo_full_name* must be
        within the robotsix-mill GitHub App's installation scope.

        Args:
            ticket_id: The blocked ticket this PR addresses (e.g.
                ``"20250624T020652Z-my-ticket-a1b2"``).
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            pr_number: The PR number to resolve the conflict on.
            resolved_files_json: JSON array of ``{"path": "...",
                "content": "..."}`` objects describing the resolved
                (merged) content of the conflicted files.  Paths are
                relative to the repo root.  May be an empty array ``[]``
                to accept the head-branch version of every file.
            commit_message: Commit message.  Defaults to a message that
                references the base/head branches and the *pr_number*.

        Returns:
            A status message with the merge commit SHA on success, or an
            error message describing why the resolution was refused or failed.

        """
        if error := await assert_blocked_and_scoped(client, ticket_id, repo_full_name):
            return error

        try:
            resolved_files: list[dict[str, str]] = json.loads(resolved_files_json)
        except json.JSONDecodeError, TypeError:
            return (
                "Error: resolved_files_json must be a valid JSON array "
                "of {path, content} objects."
            )

        if not isinstance(resolved_files, list):
            return "Error: resolved_files_json must be a JSON array."

        for f in resolved_files:
            if not isinstance(f, dict) or not f.get("path"):
                return (
                    "Error: each resolved_files_json entry must be an object "
                    "with a non-empty 'path' field."
                )

        msg = commit_message or (
            f"merge: resolve conflicts on PR #{pr_number} for blocked ticket "
            f"{ticket_id}"
        )
        return await client.resolve_pr_conflict(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            resolved_files=resolved_files,
            commit_message=msg,
        )

    return [
        check_pr_merge_conflict,
        inspect_pr_diff,
        list_open_prs,
        resolve_pr_conflict,
    ]
