"""Auto-merge pre-flight check for the refine stage.

Runs during ticket refinement (piggybacked on the inflight-advisory step)
to identify whether auto-merge will be blocked for the target repo or
expected change paths.  Posts an advisory comment when restrictions are
detected so the operator is alerted early — rather than discovering the
block only after implementation and CI pass, when the merge stage parks
the ticket in ``human_mr_approval``.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_mill.config._settings_stages import Settings
    from robotsix_mill.config.repos import RepoConfig
    from robotsix_mill.core.models import Ticket
    from robotsix_mill.stages.base import StageContext

log = logging.getLogger(__name__)

# Regex to extract file-path-looking tokens from a backtick-quoted span.
_PATH_CANDIDATE_RE = re.compile(r"`([^`]+)`")


def run_auto_merge_preflight(
    ctx: StageContext,
    ticket: Ticket,
    draft: str,
    settings: Settings,
) -> None:
    """Check auto-merge eligibility and post an advisory comment if blocked.

    Best-effort: unlike the merge-stage check this has no PR to inspect, so
    the sensitive-glob gate is based on file paths extracted from the draft
    text, and the denylist gate uses only the config-level repo identity.

    The comment is advisory — it does NOT change the ticket state.
    """
    issues: list[str] = []

    # --- Global kill switches ---
    if getattr(settings, "auto_merge_enabled", True) is False:
        issues.append("Auto-merge is globally disabled (`auto_merge_enabled: false`).")
    if getattr(settings, "auto_merge_kill_switch", False) is True:
        issues.append(
            "Auto-merge kill-switch is engaged (`auto_merge_kill_switch: true`)."
        )

    # --- Repo denylist ---
    rc: RepoConfig | None = getattr(ctx, "repo_config", None)
    denylist: list[str] = getattr(settings, "auto_merge_infra_denylist", None) or []
    if rc is not None and rc.repo_id in denylist:
        issues.append(
            f"Repo `{rc.repo_id}` is on the auto-merge infra denylist — "
            "auto-merge will be blocked."
        )

    # --- Sensitive globs (best-effort from draft text) ---
    sensitive: list[str] = getattr(settings, "auto_merge_sensitive_globs", None) or []
    if sensitive and draft:
        file_paths = _extract_file_paths_from_draft(draft)
        matched = _match_sensitive_globs(file_paths, sensitive)
        if matched:
            lines = "\n".join(f"  - `{p}` (matched glob `{g}`)" for p, g in matched)
            issues.append(
                "The draft references files matching auto-merge-sensitive "
                "globs:\n"
                + lines
                + "\n\nThese paths block auto-merge — a human operator "
                "will need to merge this PR manually."
            )

    if not issues:
        return

    # Post an advisory comment.
    comment = (
        "## :warning: Auto-merge pre-flight advisory\n\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + "\n\n*This advisory was posted during ticket refinement.  "
        "The ticket will likely park in `human_mr_approval` after "
        "implementation rather than auto-merging.*"
    )
    try:
        ctx.service.add_comment(ticket.id, comment)
    except Exception:
        log.warning(
            "%s: failed to post auto-merge pre-flight comment",
            getattr(ticket, "id", "unknown"),
            exc_info=True,
        )


def _extract_file_paths_from_draft(draft: str) -> list[str]:
    """Extract file paths from backtick-quoted spans in the draft.

    Looks for tokens that look like file paths (contain a ``/`` or end
    with a known extension) inside backtick-quoted spans.
    """
    paths: list[str] = []
    for m in _PATH_CANDIDATE_RE.finditer(draft):
        candidate = m.group(1)
        if "/" in candidate or candidate.endswith(
            (
                ".py",
                ".yml",
                ".yaml",
                ".json",
                ".md",
                ".js",
                ".ts",
                ".toml",
                ".cfg",
                ".ini",
                ".env",
                ".txt",
                ".sh",
                ".bash",
                ".dockerfile",
                ".Dockerfile",
                ".css",
                ".html",
                ".xml",
                ".svg",
                ".tf",
                ".hcl",
                ".proto",
                ".sql",
                ".graphql",
            )
        ):
            paths.append(candidate)
    return paths


def _match_sensitive_globs(paths: list[str], globs: list[str]) -> list[tuple[str, str]]:
    """Return ``(path, glob)`` for every path that matches a sensitive glob."""
    matched: list[tuple[str, str]] = []
    for path in paths:
        for g in globs:
            if fnmatch.fnmatch(path, g):
                matched.append((path, g))
                break
    return matched
