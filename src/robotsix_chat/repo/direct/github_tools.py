"""GitHub repo/PR operation tools for the direct-repo capability.

Factory :func:`build_github_tools` returns the agent-facing tool closures
that push branches, open/update/merge PRs, check CI status, recover
auto-merge, reset the implement-spawn counter, and apply patches.  It
receives the already-constructed :class:`DirectRepoClient` /
:class:`BoardClient` plus the ticket-precondition helpers from
:mod:`robotsix_chat.repo.direct`, keeping the closures thin and testable.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

    from .board_client import BoardClient
    from .client import DirectRepoClient

from .github_tools_ci import build_github_tools_ci

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


def _confirmation_echo(pr_title: str, head_base_branches: str) -> str:
    """Return the audit echo suffix for a PR mutation result.

    ``pr_title`` / ``head_base_branches`` are purely cosmetic (the operator
    confirmation lives in the conversation, not in these arguments), so they
    are optional: live 2026-10-13 10:14Z (correlation 14acad9d…) a periodic
    agent called ``merge_direct_repo_pr(repo_full_name=…, pr_number=…)`` for
    five PRs and the claude_sdk path rejected every call with "'pr_title' is
    a required property" before the body ran. Echo whatever was given.
    """
    parts: list[str] = []
    if pr_title:
        parts.append(f"PR: {pr_title}")
    if head_base_branches:
        parts.append(f"Branches: {head_base_branches}")
    return ("\n" + "\n".join(parts)) if parts else ""


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
    # Imported here (not at module scope): the group modules import the
    # validation helpers above from this module, so a top-level import would
    # be circular.
    from .github_tools_admin import build_github_tools_admin
    from .github_tools_files import build_github_tools_files
    from .github_tools_pr_inspect import build_github_tools_pr_inspect
    from .github_tools_pr_lifecycle import build_github_tools_pr_lifecycle

    (
        push_direct_repo_branch,
        apply_patch_to_file,
        push_patch_to_pr_branch,
    ) = build_github_tools_files(
        client=client,
        assert_blocked_and_scoped=assert_blocked_and_scoped,
    )
    (
        open_direct_repo_pr,
        open_simple_repo_pr,
        update_simple_repo_pr,
        update_pr_branch,
        recover_auto_merge,
        merge_direct_repo_pr,
        close_direct_repo_pr,
        check_direct_repo_auto_merge,
        arm_direct_repo_auto_merge,
    ) = build_github_tools_pr_lifecycle(
        client=client,
        settings=settings,
        component_request=component_request,
        assert_blocked_and_scoped=assert_blocked_and_scoped,
        assert_in_scope=assert_in_scope,
    )
    (
        check_pr_merge_conflict,
        inspect_pr_diff,
        list_open_prs,
        resolve_pr_conflict,
    ) = build_github_tools_pr_inspect(
        client=client,
        board=board,
        component_request=component_request,
        assert_blocked_and_scoped=assert_blocked_and_scoped,
    )
    (
        enable_repo_pages,
        get_repo_settings,
        set_repo_delete_branch_on_merge,
        reset_implement_spawn_counter,
        inspect_github_installation_token,
    ) = build_github_tools_admin(
        client=client,
        board=board,
        component_request=component_request,
        assert_in_scope=assert_in_scope,
    )

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
        get_repo_settings,
        set_repo_delete_branch_on_merge,
        reset_implement_spawn_counter,
        apply_patch_to_file,
        push_patch_to_pr_branch,
        resolve_pr_conflict,
        inspect_github_installation_token,
    ]
