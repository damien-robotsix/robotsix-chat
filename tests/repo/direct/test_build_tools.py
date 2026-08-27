"""Tests for build_direct_repo_tools."""

from __future__ import annotations

from robotsix_chat.config import DirectRepoSettings
from robotsix_chat.repo.direct import build_direct_repo_tools

from .conftest import _settings

# ---------------------------------------------------------------------------
# build_direct_repo_tools
# ---------------------------------------------------------------------------


def test_build_direct_repo_tools_disabled() -> None:
    """Verify that disabled direct_repo returns no tools."""
    assert build_direct_repo_tools(DirectRepoSettings(enabled=False)) == []


def test_build_direct_repo_tools_returns_twentytwo_tools() -> None:
    """Verify that enabled direct_repo returns the twenty-three expected tools."""
    tools = build_direct_repo_tools(_settings())
    assert len(tools) == 23
    names = [t.__name__ for t in tools]
    assert "push_direct_repo_branch" in names
    assert "open_direct_repo_pr" in names
    assert "update_pr_branch" in names
    assert "check_pr_merge_conflict" in names
    assert "verify_pr_ci_status" in names
    assert "inspect_pr_diff" in names
    assert "check_ci_health" in names
    assert "rerun_ci_workflow" in names
    assert "fetch_ci_job_logs" in names
    assert "fetch_trivy_findings" in names
    assert "file_ci_stabilization_ticket" in names
    assert "recover_auto_merge" in names
    assert "check_direct_repo_auto_merge" in names
    assert "list_open_prs" in names
    assert "merge_direct_repo_pr" in names
    assert "close_direct_repo_pr" in names
    assert "arm_direct_repo_auto_merge" in names
    assert "enable_repo_pages" in names
    assert "reset_implement_spawn_counter" in names
    assert "apply_patch_to_file" in names
    assert "push_patch_to_pr_branch" in names
    assert "resolve_pr_conflict" in names
    assert "inspect_github_installation_token" in names
