"""PR-operation methods for DirectRepoClient, split out to keep client.py focused.

Mixed into :class:`~robotsix_chat.repo.direct.client.DirectRepoClient`; ``self`` is
always a full ``DirectRepoClient`` at runtime.  The method bodies were moved
verbatim from ``client.py``; the public API is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings


class PROperationsMixin:
    """PR-operation methods mixed into :class:`DirectRepoClient`.

    The methods here call helpers and read attributes that live on
    ``DirectRepoClient``.  Those depended-on members are declared below under
    ``TYPE_CHECKING`` (annotations only, no runtime effect) so ``mypy --strict``
    can type-check the moved bodies without annotating ``self`` as a subtype.
    """

    # --- members provided by DirectRepoClient (declared for mypy --strict) ---
    if TYPE_CHECKING:
        _s: DirectRepoSettings
        _base_url: str

        async def _get_json(
            self,
            path: str,
            *,
            owner: str | None = None,
            repo: str | None = None,
        ) -> Any:
            raise NotImplementedError

        async def _post_json(self, path: str, body: dict[str, Any]) -> Any:
            raise NotImplementedError

        async def _patch_json(self, path: str, body: dict[str, Any]) -> Any:
            raise NotImplementedError

        async def _request_json(
            self,
            method: str,
            path: str,
            body: dict[str, Any],
            *,
            owner: str | None = None,
            repo: str | None = None,
        ) -> Any:
            raise NotImplementedError

        async def _http_with_retry(
            self,
            method: str,
            url: str,
            *,
            owner: str | None = None,
            repo: str | None = None,
            **kwargs: Any,
        ) -> Any:
            raise NotImplementedError

        async def _gh_headers(
            self, *, owner: str | None = None, repo: str | None = None
        ) -> dict[str, str]:
            raise NotImplementedError

        async def _git_create_tree(
            self,
            repo_full_name: str,
            base_tree_sha: str,
            files: list[dict[str, str]],
        ) -> str:
            raise NotImplementedError

        async def _search_issues(
            self,
            raw_query: str,
            *,
            per_page: int = 100,
            owner: str | None = None,
            repo: str | None = None,
        ) -> list[dict[str, Any]]:
            raise NotImplementedError

    # --- extracted PR methods (verbatim bodies) ---------------------------

    async def create_pr(
        self,
        *,
        repo_full_name: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> str:
        """Open a pull request.  No auto-merge — human review required.

        Never raises — returns a success/error message string.
        """
        try:
            # Determine base branch
            repo = await self._get_json(f"/repos/{repo_full_name}")
            default_branch = repo.get("default_branch", "main")

            pr_data = await self._post_json(
                f"/repos/{repo_full_name}/pulls",
                {
                    "title": title,
                    "body": body,
                    "head": head_branch,
                    "base": default_branch,
                },
            )
            pr_url = pr_data.get("html_url", "")
            return (
                f"Pull request opened successfully.\n"
                f"URL: {pr_url}\n"
                f"Auto-merge is NOT enabled — human review required before merge."
            )
        except RuntimeError as exc:
            return f"Error opening PR: {exc}"
        except Exception as exc:
            return f"Error opening PR: {exc}"

    async def update_pr_branch(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Update a PR branch with the latest base-branch changes (rebase).

        Calls ``PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch``
        which is equivalent to clicking the "Update branch" button on a GitHub
        PR.  GitHub attempts a rebase by default; if conflicts are detected the
        endpoint returns 422 with the conflict reason.

        Never raises — returns a success/error message string.
        """
        try:
            url = (
                f"{self._base_url}/repos/{repo_full_name}"
                f"/pulls/{pr_number}/update-branch"
            )
            owner, _, repo = repo_full_name.partition("/")
            result = await self._http_with_retry(
                "PUT",
                url,
                owner=owner or None,
                repo=repo or None,
                headers=await self._gh_headers(owner=owner or None, repo=repo or None),
                timeout=self._s.timeout,
                label="GitHub API (update-branch)",
            )
            if result.ok:
                return (
                    f"PR #{pr_number} in {repo_full_name} has been queued for "
                    f"branch update (rebase).  The update is in progress."
                )
            # 422 = unprocessable (typically merge conflict)
            if result.status_code == 422:
                detail = result.error or "(no detail)"
                return (
                    f"PR #{pr_number} in {repo_full_name} could not be updated: "
                    f"merge conflict detected.  The branch has conflicts that "
                    f"must be resolved manually.\n"
                    f"GitHub response: {detail}"
                )
            return f"Error updating PR branch: {result.error or 'unknown error'}"
        except Exception as exc:
            return f"Error updating PR branch: {exc}"

    async def resolve_pr_conflict(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        resolved_files: list[dict[str, str]],
        commit_message: str,
    ) -> str:
        """Resolve a PR's merge conflict by creating a merge commit on the head branch.

        Pushing an ordinary commit to the head branch does NOT clear a
        base↔head conflict — the base branch is still not an ancestor of the
        head.  This method clears the conflict by creating a **merge commit**
        on the PR's head branch whose parents are ``[head SHA, base SHA]``.
        Once base is an ancestor of head, GitHub recomputes the PR as
        mergeable.

        The merge commit's tree is the head commit's tree with the contents
        of *resolved_files* overlaid on top (``{"path": ..., "content": ...}``
        entries).  Conflicted paths SHOULD appear in *resolved_files* with
        their merged content; paths that are not listed keep their head-branch
        content, so an empty *resolved_files* list resolves every conflict in
        favour of the head branch.

        Steps: fetch PR → read head/base refs → overlay resolved blobs on the
        head tree → create the two-parent commit → fast-forward the head ref.

        Never raises — returns a success/error message string.
        """
        try:
            # 1. Fetch the PR and validate its state.
            pr = await self.get_pr(repo_full_name=repo_full_name, pr_number=pr_number)
        except RuntimeError as exc:
            return (
                f"Error resolving conflict on PR #{pr_number} in "
                f"{repo_full_name}: {exc}"
            )

        state = pr.get("state", "unknown")
        if state != "open":
            return (
                f"PR #{pr_number} in {repo_full_name} is {state}, not open — "
                f"conflict resolution only applies to open PRs."
            )

        # Refuse to act while GitHub is still computing mergeability; only
        # proceed when the PR is known to be in conflict.
        mergeable = pr.get("mergeable")
        if mergeable is True:
            return (
                f"PR #{pr_number} in {repo_full_name} has no merge conflict "
                f"(mergeable_state={pr.get('mergeable_state', 'unknown')}) — "
                f"nothing to resolve."
            )
        if mergeable is None:
            return (
                f"Cannot resolve conflicts on PR #{pr_number} in "
                f"{repo_full_name} yet: mergeability is still being computed "
                f"by GitHub.  Wait a few seconds and try again."
            )

        head_info = pr.get("head", {})
        head_branch = head_info.get("ref")
        head_repo = head_info.get("repo", {})
        head_repo_full_name = head_repo.get("full_name")
        base_info = pr.get("base", {})
        base_branch = base_info.get("ref")

        if not head_branch:
            return (
                f"Error: PR #{pr_number} in {repo_full_name} has no head "
                f"branch — cannot determine where to create the merge commit."
            )
        if head_repo_full_name and head_repo_full_name != repo_full_name:
            return (
                f"Refused: PR #{pr_number} head branch '{head_branch}' belongs "
                f"to '{head_repo_full_name}', not '{repo_full_name}'. "
                f"Cross-repo PR conflict resolution is not permitted."
            )
        if not base_branch:
            return (
                f"Error: PR #{pr_number} in {repo_full_name} has no base "
                f"branch — cannot determine the second merge parent."
            )

        try:
            # 2. Resolve head and base branch tip SHAs.
            head_ref = await self._get_json(
                f"/repos/{repo_full_name}/git/ref/heads/{head_branch}"
            )
            base_ref = await self._get_json(
                f"/repos/{repo_full_name}/git/ref/heads/{base_branch}"
            )
            head_sha: str = head_ref["object"]["sha"]
            base_sha: str = base_ref["object"]["sha"]
        except (KeyError, TypeError, RuntimeError) as exc:
            return (
                f"Error resolving conflict on PR #{pr_number} in "
                f"{repo_full_name}: could not read head/base branch SHAs: {exc}"
            )

        try:
            # 3. Overlay the resolved files on the head commit's tree.
            head_commit = await self._get_json(
                f"/repos/{repo_full_name}/git/commits/{head_sha}"
            )
            merged_tree_sha = await self._git_create_tree(
                repo_full_name, head_commit["tree"]["sha"], resolved_files
            )

            # 4. Create the merge commit: parents = [head SHA, base SHA].
            #    This makes base an ancestor of head, clearing the conflict.
            msg = commit_message or (
                f"merge: resolve conflicts between '{base_branch}' and "
                f"'{head_branch}' (PR #{pr_number})"
            )
            commit_data = await self._post_json(
                f"/repos/{repo_full_name}/git/commits",
                {
                    "message": msg,
                    "tree": merged_tree_sha,
                    "parents": [head_sha, base_sha],
                },
            )
            merge_commit_sha = str(commit_data["sha"])

            # 5. Fast-forward the head branch ref to the merge commit.  The
            #    merge commit descends from the current head, so the update
            #    is a fast-forward unless somebody else pushed in between.
            await self._patch_json(
                f"/repos/{repo_full_name}/git/refs/heads/{head_branch}",
                {
                    "sha": merge_commit_sha,
                    "force": False,
                },
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            return (
                f"Error resolving conflict on PR #{pr_number} in "
                f"{repo_full_name}: {exc}"
            )

        # 6. Re-check mergeability after the ref update so the returned
        #    string carries factual state, not a prediction.
        recheck = await self.get_pr(repo_full_name=repo_full_name, pr_number=pr_number)
        recheck_mergeable = recheck.get("mergeable")
        recheck_state = recheck.get("mergeable_state", "unknown")
        mergeable_label = (
            "clean"
            if recheck_mergeable is True
            else "still computing"
            if recheck_mergeable is None
            else "still conflicting"
        )

        return (
            f"Merge conflict on PR #{pr_number} in {repo_full_name} resolved.\n"
            f"Created merge commit {merge_commit_sha} on '{head_branch}' with "
            f"parents [head {head_sha[:7]}, base {base_sha[:7]}] — the base "
            f"branch '{base_branch}' is now an ancestor of the head branch.\n"
            f"Re-checked mergeable: {mergeable_label} "
            f"(mergeable_state={recheck_state})."
        )

    async def get_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> Any:
        """Return the PR object from the GitHub API.

        Raises RuntimeError on failure (callers catch and format).
        """
        return await self._get_json(f"/repos/{repo_full_name}/pulls/{pr_number}")

    async def find_open_pr_for_branch(
        self,
        *,
        repo_full_name: str,
        branch_name: str,
    ) -> Any:
        """Return the open PR whose head branch is *branch_name*, or ``None``.

        Calls ``GET /repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open``
        and returns the first matching PR object (there is at most one open PR
        per head branch), or ``None`` when no open PR targets that branch.

        Raises RuntimeError on API failure (callers catch and format).
        """
        owner = repo_full_name.split("/", 1)[0]
        prs = await self._get_json(
            f"/repos/{repo_full_name}/pulls?head={owner}:{branch_name}&state=open"
        )
        if isinstance(prs, list) and prs:
            return prs[0]
        return None

    async def get_pr_diff(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Return the raw unified diff of a pull request.

        Calls ``GET /repos/{owner}/{repo}/pulls/{pr_number}`` with the
        ``application/vnd.github.v3.diff`` media type, returning the raw
        diff text (not JSON).

        Raises RuntimeError on failure (callers catch and format).
        """
        path = f"/repos/{repo_full_name}/pulls/{pr_number}"
        url = f"{self._base_url}{path}"
        owner, _, repo = repo_full_name.partition("/")
        headers = await self._gh_headers(owner=owner or None, repo=repo or None)
        headers["Accept"] = "application/vnd.github.v3.diff"
        result = await self._http_with_retry(
            "GET",
            url,
            owner=owner or None,
            repo=repo or None,
            headers=headers,
            timeout=self._s.timeout,
            label="GitHub API",
        )
        if result.error:
            raise RuntimeError(f"GitHub API GET {path}: {result.error}")
        return result.text or ""

    async def search_prs(
        self,
        *,
        owner: str | None = None,
        repo_full_name: str | None = None,
        state: str = "all",
        since: str | None = None,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return PRs matching the scope via one ``/search/issues`` query.

        Scope is either a single repository (``repo:<owner>/<name>``) or an
        account (``user:<owner>`` — GitHub's ``user:`` qualifier matches
        both personal accounts and organisations, unlike ``org:``).
        *state* is ``"open"``, ``"closed"`` or ``"all"``; *since* is an
        ISO date (``YYYY-MM-DD``) applied as ``updated:>=<since>`` so merged
        PRs stay visible.  Paginates through the result pages (GitHub caps
        search results at 10 pages / 1000 items); results are limited to
        repositories the GitHub App installation can access.

        Raises RuntimeError on failure (callers catch and format).
        """
        terms = ["type:pr"]
        if state in ("open", "closed"):
            terms.append(f"state:{state}")
        if repo_full_name:
            terms.append(f"repo:{repo_full_name}")
        elif owner:
            terms.append(f"user:{owner}")
        else:
            raise RuntimeError("search_prs needs an owner or a repo_full_name")
        if since:
            terms.append(f"updated:>={since}")
        # Thread the repository (or account owner) so the installation token
        # can be resolved per repository when no fixed id is pinned — the
        # ``/search/issues`` path itself carries no owner/repo to derive.
        search_owner: str | None = None
        search_repo: str | None = None
        if repo_full_name:
            search_owner, _, search_repo = repo_full_name.partition("/")
        return await self._search_issues(
            " ".join(terms),
            per_page=per_page,
            owner=search_owner or owner,
            repo=search_repo or None,
        )

    async def search_open_prs(
        self,
        *,
        org_name: str,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return open PRs across *org_name*'s repositories via the Search API.

        Thin wrapper over :meth:`search_prs` kept for callers that already
        hold an organisation name (``type:pr state:open org:<org_name>``).

        Raises RuntimeError on failure (callers catch and format).
        """
        return await self._search_issues(
            f"type:pr state:open org:{org_name}",
            per_page=per_page,
            owner=org_name,
        )

    async def merge_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        merge_method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> str:
        """Merge a pull request.

        Calls ``PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge``.

        Before attempting the merge the method fetches the PR to surface
        actionable diagnostics when the merge is blocked: draft state,
        merge conflicts, or failing/pending CI checks.  GitHub enforces
        the same preconditions server-side (returns 405/409), so the
        pre-flight check is a best-effort diagnostic layer.

        Args:
            repo_full_name: ``"owner/name"``.
            pr_number: The PR number to merge.
            merge_method: ``"squash"`` (default), ``"merge"``, or ``"rebase"``.
            commit_title: Optional title for the merge commit (squash/merge only).
            commit_message: Optional body for the merge commit (squash/merge only).

        Returns:
            A success message with the merge commit SHA, or an error message.

        Never raises — returns an error string on any failure.

        """
        try:
            # --- pre-flight: fetch PR to diagnose blockers ---
            pr = await self.get_pr(repo_full_name=repo_full_name, pr_number=pr_number)
        except RuntimeError as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        # Draft check
        if pr.get("draft"):
            return (
                f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                f"the PR is still in draft state.  Mark it as ready for "
                f"review before merging."
            )

        # Mergeability check (GitHub computes this asynchronously)
        mergeable = pr.get("mergeable")
        mergeable_state = pr.get("mergeable_state", "unknown")
        if mergeable is False:
            return (
                f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                f"merge conflicts detected (mergeable_state={mergeable_state}). "
                f"Resolve conflicts or rebase the branch before merging."
            )
        if mergeable is None:
            return (
                f"Cannot merge PR #{pr_number} in {repo_full_name} yet: "
                f"mergeability is still being computed by GitHub. "
                f"Wait a few seconds and try again."
            )

        # Already merged?
        if pr.get("merged"):
            merge_sha = pr.get("merge_commit_sha", "(unknown)")
            return (
                f"PR #{pr_number} in {repo_full_name} is already merged "
                f"(merge commit: {merge_sha})."
            )

        # --- attempt the merge ---
        body: dict[str, Any] = {"merge_method": merge_method}
        if commit_title is not None:
            body["commit_title"] = commit_title
        if commit_message is not None:
            body["commit_message"] = commit_message

        try:
            result = await self._request_json(
                "PUT",
                f"/repos/{repo_full_name}/pulls/{pr_number}/merge",
                body,
            )
        except RuntimeError as exc:
            msg = str(exc)
            # GitHub returns 405 when the PR is not mergeable (e.g. CI
            # checks pending/failing, required reviews missing).
            if "405" in msg:
                return (
                    f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                    f"the PR is not in a mergeable state.  Common causes: "
                    f"required status checks are pending or failing, "
                    f"required reviews are missing, or branch protection "
                    f"rules are not satisfied.  "
                    f"GitHub response: {msg}"
                )
            if "409" in msg:
                return (
                    f"Cannot merge PR #{pr_number} in {repo_full_name}: "
                    f"merge conflict or SHA mismatch.  "
                    f"GitHub response: {msg}"
                )
            return f"Error merging PR #{pr_number} in {repo_full_name}: {msg}"

        merged = result.get("merged", False)
        sha = result.get("sha", "(unknown)")
        message = result.get("message", "")

        if merged:
            return (
                f"PR #{pr_number} in {repo_full_name} merged successfully "
                f"using {merge_method}.\n"
                f"Merge commit SHA: {sha}"
            )
        # GitHub returned 200 but merged=False — surface the message
        return (
            f"PR #{pr_number} in {repo_full_name} was not merged: "
            f"{message or 'unknown reason'} (SHA: {sha})"
        )

    async def close_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> str:
        """Close a pull request without merging.

        Calls ``PATCH /repos/{owner}/{repo}/pulls/{pull_number}`` with
        ``{"state": "closed"}``.

        Before attempting the close the method fetches the PR to surface
        actionable diagnostics when the PR is already closed or merged.

        Args:
            repo_full_name: ``"owner/name"``.
            pr_number: The PR number to close.

        Returns:
            A success message, or an error message.

        Never raises — returns an error string on any failure.

        """
        try:
            pr = await self.get_pr(repo_full_name=repo_full_name, pr_number=pr_number)
        except RuntimeError as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        state = pr.get("state", "unknown")
        if state == "closed":
            if pr.get("merged"):
                return (
                    f"PR #{pr_number} in {repo_full_name} is already "
                    f"closed (merged).  No action needed."
                )
            return (
                f"PR #{pr_number} in {repo_full_name} is already closed "
                f"(unmerged).  No action needed."
            )

        try:
            await self._patch_json(
                f"/repos/{repo_full_name}/pulls/{pr_number}",
                {"state": "closed"},
            )
        except RuntimeError as exc:
            msg = str(exc)
            return f"Error closing PR #{pr_number} in {repo_full_name}: {msg}"

        return (
            f"PR #{pr_number} in {repo_full_name} has been closed.  "
            f"The branch is preserved — it can be re-opened or a new PR "
            f"created from it later."
        )
