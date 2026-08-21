"""Fix-ticket close verification for the automated DONE transition.

CI-fix tickets (dependency fixes spawned by the CI-fix stage) exist for one
reason: to land a code change that repairs a broken main CI.  A fix ticket
that reaches DONE with no evidence of landing — no PR merged into the target
branch and no change on the ticket branch — is a false close: the implement
worker reported success but the repo never changed, so main stays red and the
deploy pipeline stays broken.

This module provides the cross-check invoked from the shadow-package
``_TransitionMixin.transition`` patch: before a fix ticket transitions to
DONE, verify that either a PR was merged into the target branch or the
ticket branch's diff against the target is non-empty.  When neither holds,
the caller raises ``TransitionError`` so the pipeline escalates to BLOCKED
for operator review instead of reporting 'done'.

Deliberately stdlib-only so the module can be imported directly from source
in tests (mirroring ``changelog_gate.py`` / ``towncrier.py``) without the
``robotsix_mill`` shadow package's sibling imports.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Any

#: Persisted ``Ticket.source`` value for CI-fix tickets spawned by the
#: ci-fix stage's out-of-scope / dependency routing.
_FIX_SOURCE_KIND = "ci_fix_dependency"

#: Title prefix used by :meth:`CIFixStage._spawn_or_reuse_fix`.
_FIX_TITLE_PREFIX = "ci_fix:"

#: Title prefix used by the recurring-CI-failure diagnostic (the "same
#: failure across N tickets" fix-proposal draft).
_RECURRING_CI_TITLE_PREFIX = "[diagnostic] recurring CI failure:"

#: Dedup-label prefix stamped on spawned fix tickets (``ci_fp:<fingerprint>``).
_FIX_LABEL_PREFIX = "ci_fp:"


def is_fix_ticket(ticket: Any) -> bool:
    """Return True when *ticket* is a CI-fix ticket.

    Matches by the persisted ``source`` kind, the deterministic ``ci_fix:``
    title prefix, the recurring-CI-failure diagnostic title prefix, or the
    ``ci_fp:`` fingerprint label — any one signal is enough, so a ticket
    spawned before one of the signals existed is still caught.
    """
    if ticket is None:
        return False
    if getattr(ticket, "source", "") == _FIX_SOURCE_KIND:
        return True
    title = getattr(ticket, "title", "") or ""
    if title.startswith(_FIX_TITLE_PREFIX) or title.startswith(
        _RECURRING_CI_TITLE_PREFIX
    ):
        return True
    labels = getattr(ticket, "labels", "") or ""
    return _FIX_LABEL_PREFIX in labels


def _run_git(repo_dir: Path, *args: str) -> str | None:
    """Run ``git`` in *repo_dir*; return stripped stdout, or ``None``.

    Never raises — a failed or missing git is "no evidence" to the caller.
    """
    with contextlib.suppress(Exception):
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_dir), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()
    return None


def _fetch_target(repo_dir: Path, target_branch: str) -> None:
    """Best-effort fetch of the latest target branch.

    The workspace clone's ``origin/<target>`` ref can lag the real remote;
    refresh it when possible so the tip comparison does not false-positive.
    Failures are ignored — the caller treats an unresolved ref as "no
    evidence" rather than crashing.
    """
    with contextlib.suppress(Exception):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_dir), "fetch", "origin", target_branch],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )


def verify_fix_ticket_landed(
    ticket: Any,
    repo_dir: Path | None,
    *,
    branch_prefix: str,
    target_branch: str,
    forge: Any = None,
) -> str | None:
    """Return an escalation note when a fix ticket has no landing evidence.

    Returns ``None`` when *ticket* is not a fix ticket, or when landing
    evidence exists.  Returns a human-readable escalation note (used as the
    ``TransitionError`` message) when the ticket is a fix ticket and neither
    a PR merged into the target branch nor a non-empty branch diff can be
    confirmed.

    ``forge`` is an optional object exposing
    ``pr_status(source_branch=...) -> dict | None`` (the ``Forge`` protocol).
    It disambiguates the single ambiguous git shape — branch tip == target
    tip — where a fast-forward merge is legitimate but an empty branch is a
    false close.
    """
    if not is_fix_ticket(ticket):
        return None

    ticket_id = getattr(ticket, "id", "") or "?"
    if repo_dir is None or not Path(repo_dir).exists():
        return (
            f"{ticket_id}: CI-fix ticket reached DONE without a workspace "
            "clone — cannot verify any code landed; escalating for review."
        )

    repo = Path(repo_dir)
    branch = getattr(ticket, "branch", None) or f"{branch_prefix}{ticket_id}"
    target = f"origin/{target_branch}"

    _fetch_target(repo, target_branch)

    branch_tip = _run_git(repo, "rev-parse", "--verify", branch)
    if not branch_tip:
        return (
            f"{ticket_id}: CI-fix ticket reached DONE but its feature branch "
            f"{branch!r} does not exist locally — no code landed; escalating "
            "for review."
        )

    target_tip = _run_git(repo, "rev-parse", "--verify", target)
    if not target_tip:
        return (
            f"{ticket_id}: CI-fix ticket reached DONE but the target branch "
            f"{target!r} cannot be resolved — cannot verify a merge; "
            "escalating for review."
        )

    # Landing evidence: branch and target tips differ.  This covers both
    # "a PR was merged into main" (target advanced past the branch) and
    # "the branch's diff actually changed" (the branch carries commits the
    # target lacks).
    if branch_tip != target_tip:
        return None

    # Branch tip == target tip: either a fast-forward merge (legitimate) or
    # an empty branch (false close).  A merged PR is the disambiguator.
    if forge is not None:
        try:
            pr = forge.pr_status(source_branch=branch)
        except Exception:
            pr = None
        if pr and pr.get("merged"):
            return None

    return (
        f"{ticket_id}: CI-fix ticket reached DONE but branch {branch!r} is "
        f"identical to {target!r} — no PR was merged and no diff landed; "
        "escalating for operator review instead of reporting done."
    )


__all__ = ["is_fix_ticket", "verify_fix_ticket_landed"]
