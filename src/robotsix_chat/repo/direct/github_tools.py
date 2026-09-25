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
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

    from .board_client import BoardClient
    from .client import DirectRepoClient

from .github_tools_ci import build_github_tools_ci

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"github\.com/([^/\s]+/[^/\s#?]+)/pull/(\d+)")

_PR_REF_HELP = (
    "pass `repo_full_name` (owner/name) + `pr_number`, or `pr_url` "
    "(https://github.com/owner/name/pull/N), or `ticket_id` of a mill ticket "
    "that has an open PR"
)


async def resolve_pr_ref(
    board: Any,
    *,
    repo_full_name: str | None,
    pr_number: int | None,
    repo: str | None,
    pr_url: str | None,
    ticket_id: str | None,
) -> tuple[str, int] | str:
    """Normalise the PR-reference shapes models send into ``(repo, number)``.

    Live 2026-09-08 a board-gates-drain run burned 12 turns on
    ``verify_pr_ci_status`` / ``inspect_pr_diff`` schema rejections: it tried
    ``repo`` + ``pr_number``, ``pr_url`` and ``ticket_id`` before guessing
    ``repo_full_name``.  On the claude_sdk path the JSON schema is validated
    before the tool body, so the aliases must be real parameters.

    Precedence: an explicit ``pr_url`` wins; else ``ticket_id`` is resolved
    through the board's ``pr_url``; else ``repo_full_name``/``repo`` +
    ``pr_number``.  A bare repo name (no ``/``) is resolved through the
    board's repo roster.  Returns an ``Error:`` string when nothing usable
    was supplied.
    """
    if pr_url:
        m = _PR_URL_RE.search(pr_url)
        if not m:
            return (
                f"Error: could not parse a GitHub PR url from {pr_url!r} — "
                f"{_PR_REF_HELP}."
            )
        return m.group(1), int(m.group(2))
    if ticket_id and pr_number is None:
        data = (
            await board.get_ticket_data(ticket_id)
            if hasattr(board, "get_ticket_data")
            else None
        )
        url = (data or {}).get("pr_url") if isinstance(data, dict) else None
        m = _PR_URL_RE.search(url or "")
        if not m:
            return (
                f"Error: ticket {ticket_id} has no open PR url on the board "
                f"(state may be closed or pre-implement) — {_PR_REF_HELP}."
            )
        return m.group(1), int(m.group(2))
    name = repo_full_name or repo
    if not name or pr_number is None:
        return f"Error: PR reference incomplete — {_PR_REF_HELP}."
    if "/" not in name:
        resolved = (
            await board.resolve_repo_full_name(name)
            if hasattr(board, "resolve_repo_full_name")
            else None
        )
        if not resolved:
            return (
                f"Error: {name!r} is not an owner/name repo and is not on the "
                f"board roster — {_PR_REF_HELP}."
            )
        name = resolved
    return name, int(pr_number)


# Accepted per-file entry forms for ``files_json``.  Each entry is an
# object with a ``path`` plus exactly ONE content source:
#   {"path": "...", "content": "..."}      — text, committed as-is
#   {"path": "...", "content_b64": "..."}  — base64 bytes (binary files)
#   {"path": "...", "local_path": "..."}   — read bytes from a file inside
#                                            the file-hub work directory
FILES_JSON_FORMS = (
    "{path, content} (text), {path, content_b64} (base64 bytes), or "
    "{path, local_path} (a file inside the file-hub work directory)"
)


def validate_file_entries(files: list[Any]) -> str | None:
    """Return an error string if any ``files_json`` entry is malformed.

    Each entry must be an object carrying a non-empty ``path`` and exactly
    one content source (``content``, ``content_b64``, or ``local_path``).
    Returns ``None`` when every entry is well-formed.
    """
    for f in files:
        if not isinstance(f, dict):
            return f"Error: files_json entries must be objects: {FILES_JSON_FORMS}."
        if not f.get("path"):
            return "Error: each files_json entry must have a non-empty 'path'."
        sources = [k for k in ("content", "content_b64", "local_path") if k in f]
        if len(sources) != 1:
            found = ", ".join(sources) if sources else "none"
            return (
                f"Error: files_json entry for '{f.get('path')}' must carry "
                f"exactly one content source — {FILES_JSON_FORMS} "
                f"(found: {found})."
            )
    return None


# Accepted per-file entry forms for a changeset that also supports deletion
# (used by ``update_simple_repo_pr``).  Each entry carries a ``path`` plus
# EITHER exactly one content source OR ``delete: true``:
#   {"path": "...", "content": "..."}      — text, committed as-is
#   {"path": "...", "content_b64": "..."}  — base64 bytes (binary files)
#   {"path": "...", "local_path": "..."}   — read bytes from a file inside
#                                            the file-hub work directory
#   {"path": "...", "delete": true}        — remove the path from the tree
FILES_JSON_CHANGESET_FORMS = (
    "{path, content} (text), {path, content_b64} (base64 bytes), "
    "{path, local_path} (a file inside the file-hub work directory), or "
    "{path, delete: true} (remove the path)"
)


def validate_changeset_entries(files: list[Any]) -> str | None:
    """Return an error string if any changeset entry is malformed.

    Each entry must be an object carrying a non-empty ``path`` and EITHER a
    ``delete: true`` flag OR exactly one content source (``content``,
    ``content_b64``, or ``local_path``).  A delete entry must NOT also carry a
    content source.  Returns ``None`` when every entry is well-formed.
    """
    for f in files:
        if not isinstance(f, dict):
            return (
                f"Error: files_json entries must be objects: "
                f"{FILES_JSON_CHANGESET_FORMS}."
            )
        if not f.get("path"):
            return "Error: each files_json entry must have a non-empty 'path'."
        sources = [k for k in ("content", "content_b64", "local_path") if k in f]
        if f.get("delete"):
            if sources:
                return (
                    f"Error: files_json delete entry for '{f.get('path')}' must "
                    f"not also carry a content source — "
                    f"{FILES_JSON_CHANGESET_FORMS}."
                )
            continue
        if len(sources) != 1:
            found = ", ".join(sources) if sources else "none"
            return (
                f"Error: files_json entry for '{f.get('path')}' must carry "
                f"exactly one content source or 'delete: true' — "
                f"{FILES_JSON_CHANGESET_FORMS} (found: {found})."
            )
    return None


# ---------------------------------------------------------------------------
# Ungated "simple PR" path — risky-file guard
# ---------------------------------------------------------------------------
# The ungated direct-PR path (``open_simple_repo_pr``) is only for low-risk,
# simple changes (content/text edits, small doc changes, single-file tweaks).
# Files that can alter CI behaviour or carry credentials are NOT eligible on
# this path — they must go through the ticket-gated flow (with its BLOCKED
# state check and operator review) instead.  The opened PR is the review gate
# for everything else.
SIMPLE_PR_FORBIDDEN_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
)
SIMPLE_PR_FORBIDDEN_SUFFIXES = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
)
SIMPLE_PR_FORBIDDEN_BASENAMES = (
    "id_rsa",
    "id_ed25519",
    "id_dsa",
    "id_ecdsa",
)


def check_simple_pr_file_safety(files: list[dict[str, str]]) -> str | None:
    """Return an error if any path is ineligible for the ungated simple-PR path.

    Rejects CI workflow / composite-action files and credential-shaped files
    (private keys, ``.env`` files).  Such changes carry elevated risk
    (secret exfiltration, CI compromise) and must be routed through the
    ticket-gated direct-repo flow — with its BLOCKED-state gate and operator
    review — rather than the lightweight ungated path.  Returns ``None`` when
    every path is eligible.
    """
    for f in files:
        raw = str(f.get("path", "")).strip()
        # Normalise separators and leading slashes so a guard cannot be
        # bypassed with ``\`` or a leading ``/``.
        norm = raw.replace("\\", "/").lstrip("/")
        lower = norm.lower()
        base = lower.rsplit("/", 1)[-1]
        if any(lower.startswith(prefix) for prefix in SIMPLE_PR_FORBIDDEN_PREFIXES):
            return (
                f"Refused: '{raw}' is a CI workflow/action file and is NOT "
                "eligible for the ungated simple-PR path.  Route workflow "
                "changes through the ticket-gated direct-repo flow instead."
            )
        if any(lower.endswith(suffix) for suffix in SIMPLE_PR_FORBIDDEN_SUFFIXES):
            return (
                f"Refused: '{raw}' looks like a credential/secret file and is "
                "NOT eligible for the ungated simple-PR path.  Route "
                "secret-bearing changes through the ticket-gated flow instead."
            )
        if (
            base in SIMPLE_PR_FORBIDDEN_BASENAMES
            or base == ".env"
            or (base.startswith(".env.") and not base.endswith(".example"))
        ):
            return (
                f"Refused: '{raw}' looks like a credential/secret file and is "
                "NOT eligible for the ungated simple-PR path.  Route "
                "secret-bearing changes through the ticket-gated flow instead."
            )
    return None


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

    async def close_direct_repo_pr(
        repo_full_name: str,
        pr_number: int,
        pr_title: str,
        head_base_branches: str,
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
            pr_title: The PR title — used for the confirmation echo.
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
        mode = details.get("installation_mode", "per_repository")
        configured_exists = details.get("configured_installation_exists")
        mode_label = (
            "per-repository resolution (no fixed installation id — the "
            "installation is resolved from each repo, so a re-install needs "
            "no config change)"
            if mode == "per_repository"
            else "override (a fixed `github_app_installation_id` is pinned)"
        )
        configured_display = f"`{configured_id}`" if configured_id else "`` (empty)"
        lines = [
            f"GitHub App installation token diagnostic for `{repo_full_name}`:",
            f"- App id: `{details['app_id']}`",
            f"- Installation mode: {mode_label}",
            f"- Configured installation id: {configured_display}",
            f"- Resolved installation id (for `{repo_full_name}`): `{resolved_id}`",
            f"- Token expires at: `{details['expires_at']}` (UTC)",
            f"- Seconds remaining: {details['seconds_remaining']}",
        ]
        if configured_id and mode == "override":
            if configured_exists is False:
                lines.append(
                    "  ⚠️ The pinned installation id no longer exists on GitHub "
                    "(minting returned HTTP 404). Clear "
                    "`github_app_installation_id` to resolve per repository, or "
                    "update it to the current installation id."
                )
            elif configured_exists is True:
                lines.append("  ✓ The pinned installation id still exists.")
            else:
                lines.append(
                    "  (could not confirm whether the pinned installation "
                    "still exists.)"
                )
        elif configured_id and mode == "per_repository":
            lines.append(
                "  ⚠️ The pinned installation id returned HTTP 404 earlier this "
                "process and has been abandoned — tokens are now resolved per "
                "repository. Clear `github_app_installation_id` from config."
            )
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

    _ci_tools = build_github_tools_ci(
        client=client,
        board=board,
        settings=settings,
        component_request=component_request,
        resolve_pr_ref=resolve_pr_ref,
    )
    (
        check_ci_health,
        rerun_ci_workflow,
        fetch_ci_job_logs,
        fetch_trivy_findings,
        file_ci_stabilization_ticket,
        verify_pr_ci_status,
    ) = _ci_tools
    return [
        push_direct_repo_branch,
        open_direct_repo_pr,
        open_simple_repo_pr,
        update_simple_repo_pr,
        update_pr_branch,
        check_pr_merge_conflict,
        verify_pr_ci_status,
        inspect_pr_diff,
        check_ci_health,
        rerun_ci_workflow,
        fetch_ci_job_logs,
        fetch_trivy_findings,
        file_ci_stabilization_ticket,
        recover_auto_merge,
        check_direct_repo_auto_merge,
        list_open_prs,
        merge_direct_repo_pr,
        close_direct_repo_pr,
        arm_direct_repo_auto_merge,
        enable_repo_pages,
        reset_implement_spawn_counter,
        apply_patch_to_file,
        push_patch_to_pr_branch,
        resolve_pr_conflict,
        inspect_github_installation_token,
    ]
