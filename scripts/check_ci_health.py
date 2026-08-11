#!/usr/bin/env python3
r"""Check a GitHub repo's CI health on a branch (default ``main``).

Queries the GitHub REST API for the latest commit's combined status and
check runs, plus optionally the latest run of a specific Actions workflow.
Exits 0 when all checks pass, 1 when any check fails or is pending, and 2
on API / network errors.

Intended as a pre-deploy gate: call it before spawning a deploy ticket to
verify the target repo's CI is green, so the assistant doesn't waste turns
diagnosing pre-existing failures.

Usage::

    uv run python scripts/check_ci_health.py owner/repo
    uv run python scripts/check_ci_health.py owner/repo --branch main
    uv run python scripts/check_ci_health.py owner/repo --workflow release-image.yml

Environment variables::

    GITHUB_TOKEN    Personal access token or installation token for the
                    GitHub API.  Optional — unauthenticated requests have
                    a lower rate limit (60/hour).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NoReturn

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_API_BASE = os.environ.get("GITHUB_API_URL", "https://api.github.com")
_TIMEOUT = float(os.environ.get("CHECK_CI_TIMEOUT", "30"))
_TOKEN = os.environ.get("GITHUB_TOKEN", "")

_HEADERS: dict[str, str] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if _TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_TOKEN}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_result(owner: str, repo: str, branch: str, sha: str = "") -> dict[str, Any]:
    return {
        "repo": f"{owner}/{repo}",
        "branch": branch,
        "sha": sha,
        "status": "unknown",
        "conclusion": "unknown",
        "state_counts": {},
        "failing_checks": [],
        "pending_checks": [],
        "workflow": {},
        "error": "",
    }


def _check_rate_limit(resp: httpx.Response) -> None:
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) < 5:
        reset_ts = resp.headers.get("X-RateLimit-Reset", "?")
        print(
            f"::warning::GitHub API rate limit nearly exhausted"
            f" (remaining={remaining}, reset={reset_ts})",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def _get_latest_commit(
    client: httpx.Client, owner: str, repo: str, branch: str
) -> tuple[str, str | None]:
    """Return (sha, error) for the latest commit on *branch*."""
    url = f"{_API_BASE}/repos/{owner}/{repo}/branches/{branch}"
    try:
        resp = client.get(url)
    except httpx.RequestError as exc:
        return "", f"network error fetching branch info: {exc}"
    _check_rate_limit(resp)
    if resp.status_code == 404:
        return "", f"branch {branch!r} not found in {owner}/{repo}"
    if resp.status_code >= 400:
        return "", f"GitHub API error ({resp.status_code}): {resp.text[:500]}"
    try:
        data = resp.json()
    except ValueError:
        return "", f"invalid JSON from GitHub API: {resp.text[:500]}"
    return data["commit"]["sha"], None


def _get_combined_status(
    client: httpx.Client, owner: str, repo: str, sha: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (status_data, error) for the combined commit status."""
    url = f"{_API_BASE}/repos/{owner}/{repo}/commits/{sha}/status"
    try:
        resp = client.get(url)
    except httpx.RequestError as exc:
        return None, f"network error fetching commit status: {exc}"
    _check_rate_limit(resp)
    if resp.status_code >= 400:
        return None, f"GitHub API error ({resp.status_code}): {resp.text[:500]}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"invalid JSON from GitHub API: {resp.text[:500]}"


def _get_check_runs(
    client: httpx.Client, owner: str, repo: str, sha: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (check_runs_data, error)."""
    url = f"{_API_BASE}/repos/{owner}/{repo}/commits/{sha}/check-runs"
    # Ask for the maximum page size to reduce pagination needs.
    params = {"per_page": 100}
    try:
        resp = client.get(url, params=params)
    except httpx.RequestError as exc:
        return None, f"network error fetching check runs: {exc}"
    _check_rate_limit(resp)
    if resp.status_code >= 400:
        return None, f"GitHub API error ({resp.status_code}): {resp.text[:500]}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"invalid JSON from GitHub API: {resp.text[:500]}"


def _get_workflow_runs(
    client: httpx.Client,
    owner: str,
    repo: str,
    branch: str,
    workflow_file: str,
) -> tuple[dict[str, Any] | None, str | None]:
    url = f"{_API_BASE}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
    params: dict[str, str | int] = {"branch": branch, "per_page": 1}
    try:
        resp = client.get(url, params=params)
    except httpx.RequestError as exc:
        return None, f"network error fetching workflow runs: {exc}"
    _check_rate_limit(resp)
    if resp.status_code == 404:
        return None, f"workflow file {workflow_file!r} not found in {owner}/{repo}"
    if resp.status_code >= 400:
        return None, f"GitHub API error ({resp.status_code}): {resp.text[:500]}"
    try:
        return resp.json(), None
    except ValueError:
        return None, f"invalid JSON from GitHub API: {resp.text[:500]}"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def _analyse_status(status_data: dict[str, Any], result: dict[str, Any]) -> None:
    """Populate *result* from the combined-status response."""
    state = status_data.get("state", "unknown")
    result["status"] = state
    result["state_counts"] = {
        "success": 0,
        "failure": 0,
        "pending": 0,
        "error": 0,
    }

    for s in status_data.get("statuses", []):
        ctx = s.get("context", "?")
        st = s.get("state", "unknown")
        if st in result["state_counts"]:
            result["state_counts"][st] += 1

        if st == "failure" or st == "error":
            result["failing_checks"].append(
                {
                    "context": ctx,
                    "state": st,
                    "description": s.get("description", ""),
                    "target_url": s.get("target_url", ""),
                }
            )
        elif st == "pending":
            result["pending_checks"].append(
                {
                    "context": ctx,
                    "state": st,
                    "description": s.get("description", ""),
                    "target_url": s.get("target_url", ""),
                }
            )

    if result["failing_checks"]:
        result["conclusion"] = "failure"
    elif result["pending_checks"]:
        result["conclusion"] = "pending"
    elif state == "success":
        result["conclusion"] = "success"
    else:
        result["conclusion"] = state


def _analyse_check_runs(check_data: dict[str, Any], result: dict[str, Any]) -> None:
    """Merge check-runs data into *result* (augments combined status)."""
    for run in check_data.get("check_runs", []):
        name = run.get("name", "?")
        conclusion = run.get("conclusion")  # None while in-progress
        status = run.get("status")  # queued, in_progress, completed

        if status == "completed" and conclusion == "failure":
            # Avoid duplicates with combined status.
            if not any(c["context"] == name for c in result["failing_checks"]):
                result["failing_checks"].append(
                    {
                        "context": name,
                        "state": "failure",
                        "description": run.get("output", {}).get("title", ""),
                        "target_url": run.get("html_url", ""),
                    }
                )
        elif status in ("queued", "in_progress") and not any(
            c["context"] == name for c in result["pending_checks"]
        ):
            result["pending_checks"].append(
                {
                    "context": name,
                    "state": status,
                    "description": "",
                    "target_url": run.get("html_url", ""),
                }
            )

    # Recompute conclusion from combined data.
    if result["failing_checks"]:
        result["conclusion"] = "failure"
    elif result["pending_checks"]:
        result["conclusion"] = "pending"
    elif result["conclusion"] not in ("failure", "pending"):
        result["conclusion"] = "success"


def _analyse_workflow(wf_data: dict[str, Any], result: dict[str, Any]) -> None:
    """Populate result['workflow'] from the workflow-runs response."""
    runs = wf_data.get("workflow_runs", [])
    if not runs:
        result["workflow"] = {"latest_run": None, "note": "no runs found"}
        return

    latest = runs[0]
    result["workflow"] = {
        "latest_run": {
            "id": latest.get("id"),
            "name": latest.get("name", ""),
            "status": latest.get("status"),
            "conclusion": latest.get("conclusion"),
            "html_url": latest.get("html_url", ""),
            "created_at": latest.get("created_at", ""),
        }
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _resolve_args() -> tuple[str, str, str, str | None]:
    """Return (owner, repo, branch, workflow_file)."""
    argv = sys.argv[1:]
    owner_repo = ""
    branch = "main"
    workflow_file: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--branch", "-b"):
            i += 1
            if i >= len(argv):
                print(
                    "check_ci_health: --branch requires a value",
                    file=sys.stderr,
                )
                sys.exit(2)
            branch = argv[i]
        elif arg.startswith("--branch="):
            branch = arg.split("=", 1)[1]
        elif arg in ("--workflow", "-w"):
            i += 1
            if i >= len(argv):
                print(
                    "check_ci_health: --workflow requires a value",
                    file=sys.stderr,
                )
                sys.exit(2)
            workflow_file = argv[i]
        elif arg.startswith("--workflow="):
            workflow_file = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            owner_repo = arg
        i += 1

    if not owner_repo:
        print(
            "check_ci_health: missing repo argument (owner/repo)",
            file=sys.stderr,
        )
        print(
            "usage: check_ci_health [--branch BRANCH] [--workflow FILE] OWNER/REPO",
            file=sys.stderr,
        )
        sys.exit(2)

    if "/" not in owner_repo:
        print(
            f"check_ci_health: repo must be in owner/repo format, got {owner_repo!r}",
            file=sys.stderr,
        )
        sys.exit(2)

    owner, _, repo = owner_repo.partition("/")
    return owner, repo, branch, workflow_file


def main() -> NoReturn:
    """Run the CI health check and exit 0 (pass), 1 (fail/pending), or 2 (error)."""
    owner, repo, branch, workflow_file = _resolve_args()
    result = _build_result(owner, repo, branch)

    with httpx.Client(
        timeout=_TIMEOUT,
        headers=_HEADERS,
        follow_redirects=False,
    ) as client:
        # 1. Resolve latest commit on the branch.
        sha, err = _get_latest_commit(client, owner, repo, branch)
        if err:
            result["error"] = err
            result["status"] = "error"
            result["conclusion"] = "error"
            print(json.dumps(result, indent=2, ensure_ascii=False))
            print(f"\n::error::CI health check failed: {err}", file=sys.stderr)
            sys.exit(2)
        result["sha"] = sha

        # 2. Fetch combined commit status.
        status_data, err = _get_combined_status(client, owner, repo, sha)
        if err:
            result["error"] = err
            result["status"] = "error"
            result["conclusion"] = "error"
            print(json.dumps(result, indent=2, ensure_ascii=False))
            print(f"\n::error::CI health check failed: {err}", file=sys.stderr)
            sys.exit(2)

        if status_data:
            _analyse_status(status_data, result)

        # 3. Fetch check runs for more detail.
        check_data, err = _get_check_runs(client, owner, repo, sha)
        if err:
            # Non-fatal: combined status is the primary signal.
            print(
                f"::warning::Could not fetch check runs: {err}",
                file=sys.stderr,
            )
        elif check_data:
            _analyse_check_runs(check_data, result)

        # 4. Optionally check a specific workflow.
        if workflow_file:
            wf_data, err = _get_workflow_runs(
                client, owner, repo, branch, workflow_file
            )
            if err:
                print(
                    f"::warning::Could not fetch workflow runs: {err}",
                    file=sys.stderr,
                )
            elif wf_data:
                _analyse_workflow(wf_data, result)

    # Print result and exit.
    print(json.dumps(result, indent=2, ensure_ascii=False))

    conclusion = result["conclusion"]
    if conclusion == "success":
        print(
            f"\n::notice::CI health check PASSED for {owner}/{repo}@{branch}",
            file=sys.stderr,
        )
        sys.exit(0)
    elif conclusion in ("failure", "pending"):
        failing = len(result["failing_checks"])
        pending = len(result["pending_checks"])
        parts: list[str] = []
        if failing:
            parts.append(f"{failing} failing")
        if pending:
            parts.append(f"{pending} pending")
        detail = ", ".join(parts)
        print(
            f"\n::error::CI health check FAILED for {owner}/{repo}@{branch} — {detail}",
            file=sys.stderr,
        )
        for check in result["failing_checks"]:
            print(
                f"  FAIL  {check['context']}: {check['description']}",
                file=sys.stderr,
            )
        for check in result["pending_checks"]:
            print(
                f"  PEND  {check['context']}",
                file=sys.stderr,
            )
        sys.exit(1)
    else:
        print(
            f"\n::error::CI health check UNKNOWN for {owner}/{repo}@{branch}"
            f" — state={conclusion!r}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
