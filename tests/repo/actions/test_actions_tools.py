"""Tests for the GitHub Actions LLM tools."""

from __future__ import annotations

import pytest
import respx

from robotsix_chat.config import DirectRepoSettings, GitHubActionsSettings
from robotsix_chat.repo.actions import (
    build_github_actions_tools,
    load_github_actions_skill,
)


def _actions_settings(**kw: object) -> GitHubActionsSettings:
    base: dict[str, object] = {
        "enabled": True,
        "github_org": "damien-robotsix",
    }
    base.update(kw)
    return GitHubActionsSettings(**base)  # type: ignore[arg-type]


def _direct_repo_settings(**kw: object) -> DirectRepoSettings:
    base: dict[str, object] = {
        "enabled": True,
        "github_app_id": "12345",
        "github_app_private_key": "fake-key",  # pragma: allowlist secret
        "github_app_installation_id": "67890",
        "board_api_base_url": "http://127.0.0.1:8077",
    }
    base.update(kw)
    return DirectRepoSettings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _mock_github_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock mint_installation_token so the shared library is never imported."""
    import sys
    from types import SimpleNamespace

    def _fake_mint(**kw: object) -> object:
        return SimpleNamespace(token="ghs_test_installation_token")

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)


# ---------------------------------------------------------------------------
# build_github_actions_tools
# ---------------------------------------------------------------------------


def test_build_github_actions_tools_disabled() -> None:
    """Disabled github_actions returns no tools."""
    assert (
        build_github_actions_tools(
            GitHubActionsSettings(enabled=False), _direct_repo_settings()
        )
        == []
    )


def test_build_github_actions_tools_returns_five_tools() -> None:
    """Enabled github_actions returns five tools.

    set_actions_secret, dispatch_workflow, check_workflow_run,
    fetch_workflow_run_annotations, and fetch_job_log.
    """
    tools = build_github_actions_tools(_actions_settings(), _direct_repo_settings())
    assert len(tools) == 5
    names = {getattr(f, "__name__", str(f)) for f in tools}
    assert names == {
        "set_actions_secret",
        "dispatch_workflow",
        "check_workflow_run",
        "fetch_workflow_run_annotations",
        "fetch_job_log",
    }


# ---------------------------------------------------------------------------
# set_actions_secret — scope check (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_actions_secret_refuses_out_of_scope_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → refused message."""
    dr = _direct_repo_settings()

    # Mock list-installation-repos to return only one repo
    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/allowed-repo"}]})

    tools = build_github_actions_tools(_actions_settings(), dr)
    set_secret = tools[0]

    result = await set_secret("other-repo", "MY_SECRET", "value")
    assert "not installed" in result.lower()
    assert "other-repo" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_refuses_out_of_scope_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → refused message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/allowed-repo"}]})

    tools = build_github_actions_tools(_actions_settings(), dr)
    dispatch = tools[1]

    result = await dispatch("other-repo", "deploy.yml", ref="main")
    assert "not installed" in result.lower()
    assert "other-repo" in result


# ---------------------------------------------------------------------------
# dispatch_workflow — inputs parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_workflow_rejects_invalid_inputs_json(
    respx_mock: respx.MockRouter,
) -> None:
    """Non-JSON inputs string → error message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})

    tools = build_github_actions_tools(_actions_settings(), dr)
    dispatch = tools[1]

    result = await dispatch("test-repo", "deploy.yml", inputs="not json")
    assert "Error" in result
    assert "inputs" in result.lower() or "JSON" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_rejects_non_object_inputs(
    respx_mock: respx.MockRouter,
) -> None:
    """Inputs that parse but are not a dict → error message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})

    tools = build_github_actions_tools(_actions_settings(), dr)
    dispatch = tools[1]

    result = await dispatch("test-repo", "deploy.yml", inputs='["a", "list"]')
    assert "Error" in result


# ---------------------------------------------------------------------------
# load_github_actions_skill
# ---------------------------------------------------------------------------


def test_load_github_actions_skill_returns_string() -> None:
    """The shipped skill.md is readable and non-empty."""
    skill = load_github_actions_skill()
    assert isinstance(skill, str)
    assert len(skill) > 0
    assert "PUT /chat/github/repos" in skill
    assert "workflow_dispatch" in skill


# ---------------------------------------------------------------------------
# check_workflow_run — scope check (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_workflow_run_refuses_out_of_scope_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → refused message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/allowed-repo"}]})

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("other-repo")
    assert "not installed" in result.lower()
    assert "other-repo" in result


# ---------------------------------------------------------------------------
# check_workflow_run — no recent runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_workflow_run_no_recent_runs(
    respx_mock: respx.MockRouter,
) -> None:
    """No recent workflow runs → descriptive message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs"
        )
    ).respond(json={"workflow_runs": []})

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo")
    assert "no recent workflow runs" in result.lower()


# ---------------------------------------------------------------------------
# check_workflow_run — billing-failure detection (never-started runs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_workflow_run_detects_never_started_run(
    respx_mock: respx.MockRouter,
) -> None:
    """Run with conclusion=failure and no run_started_at → billing diagnostic."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs"
        )
    ).respond(
        json={
            "workflow_runs": [
                {
                    "id": 67890,
                    "name": "Deploy",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_branch": "main",
                    "event": "workflow_dispatch",
                    "run_started_at": None,
                }
            ]
        }
    )
    # Mock repo visibility check → private repo
    respx_mock.get(
        url=f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
    ).respond(json={"private": True})

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo")
    assert "billing" in result.lower()
    assert "never started" in result.lower()
    assert "67890" in result


@pytest.mark.asyncio
async def test_check_workflow_run_detects_never_started_run_public_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Never-started run on a public repo → no billing, trigger guidance."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs"
        )
    ).respond(
        json={
            "workflow_runs": [
                {
                    "id": 67890,
                    "name": "Deploy",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_branch": "main",
                    "event": "workflow_dispatch",
                    "run_started_at": None,
                }
            ]
        }
    )
    # Mock repo visibility check → public repo
    respx_mock.get(
        url=f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
    ).respond(json={"private": False})

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo")
    assert "public" in result.lower()
    assert "billing is not the issue" in result.lower()
    assert "reusable workflow" in result.lower()
    assert "never started" in result.lower()
    assert "67890" in result


# ---------------------------------------------------------------------------
# check_workflow_run — healthy runs (no billing diagnostic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_workflow_run_healthy_runs(
    respx_mock: respx.MockRouter,
) -> None:
    """Runs with success or in_progress → summary, no billing diagnostic."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs"
        )
    ).respond(
        json={
            "workflow_runs": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "event": "push",
                },
                {
                    "id": 2,
                    "name": "Lint",
                    "status": "in_progress",
                    "conclusion": None,
                    "head_branch": "main",
                    "event": "push",
                },
            ]
        }
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo")
    assert "Recent workflow runs" in result
    assert "billing" not in result.lower()


# ---------------------------------------------------------------------------
# check_workflow_run — specific run_id (deep inspection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_workflow_run_specific_run_with_jobs(
    respx_mock: respx.MockRouter,
) -> None:
    """Specific run_id → fetches jobs and returns summary."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/42/jobs"
        )
    ).respond(
        json={
            "jobs": [
                {
                    "id": 100,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "id": 101,
                    "name": "test",
                    "status": "completed",
                    "conclusion": "skipped",
                },
            ]
        }
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", run_id=42)
    assert "42" in result
    assert "build" in result
    assert "failure" in result
    assert "test" in result
    assert "skipped" in result


@pytest.mark.asyncio
async def test_check_workflow_run_specific_run_no_jobs(
    respx_mock: respx.MockRouter,
) -> None:
    """Specific run_id with zero jobs → billing diagnostic (private repo)."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/99/jobs"
        )
    ).respond(json={"jobs": []})
    # Mock repo visibility check → private repo
    respx_mock.get(
        url=f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
    ).respond(json={"private": True})

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", run_id=99)
    assert "billing" in result.lower()
    assert "99" in result
    assert "no jobs" in result.lower()


@pytest.mark.asyncio
async def test_check_workflow_run_specific_run_no_jobs_public_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Specific run_id with zero jobs on a public repo → no billing mention."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/99/jobs"
        )
    ).respond(json={"jobs": []})
    # Mock repo visibility check → public repo
    respx_mock.get(
        url=f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
    ).respond(json={"private": False})

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", run_id=99)
    assert "public" in result.lower()
    assert "billing is not the issue" in result.lower()
    assert "reusable workflow" in result.lower()
    assert "99" in result
    assert "no jobs" in result.lower()


@pytest.mark.asyncio
async def test_check_workflow_run_specific_run_no_jobs_visibility_none(
    respx_mock: respx.MockRouter,
) -> None:
    """Specific run_id with zero jobs and visibility check failing → neutral."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/99/jobs"
        )
    ).respond(json={"jobs": []})
    # Mock repo visibility check → error (returns None)
    respx_mock.get(
        url=f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
    ).respond(status_code=500, json={"error": "Server Error"})

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", run_id=99)
    # Must NOT claim repo is public
    assert "public repository" not in result.lower()
    assert "billing is not the issue" not in result.lower()
    # Neutral fallback: mentions billing as a possibility
    assert "billing" in result.lower()
    assert "99" in result
    assert "no jobs" in result.lower()


@pytest.mark.asyncio
async def test_check_workflow_run_startup_failure_classified_per_workflow_config(
    respx_mock: respx.MockRouter,
) -> None:
    """Run with startup_failure + executed sibling → per-workflow config, no billing."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    # Jobs: empty.
    respx_mock.get(
        url=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/42/jobs"
        )
    ).respond(json={"jobs": []})
    # Single-run metadata: startup_failure with head_sha.
    respx_mock.get(
        url=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "workflow_id": 11,
            "status": "completed",
            "conclusion": "startup_failure",
            "head_sha": "abc123",
        }
    )
    # Sibling listing: Lint reached job execution on the same commit.
    respx_mock.get(
        url=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs?per_page=30&head_sha=abc123"
        )
    ).respond(
        json={
            "workflow_runs": [
                {
                    "id": 42,
                    "name": "CI",
                    "workflow_id": 11,
                    "status": "completed",
                    "conclusion": "startup_failure",
                    "head_sha": "abc123",
                },
                {
                    "id": 43,
                    "name": "Lint",
                    "workflow_id": 12,
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "abc123",
                },
                {
                    "id": 44,
                    "name": "Docs",
                    "workflow_id": 13,
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": "abc123",
                },
            ]
        }
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", run_id=42)
    assert "2 sibling workflow(s)" in result
    assert "Lint" in result
    assert "not an account-level problem" in result
    assert "root cause is in this workflow's own file" in result.lower()
    # Must NOT mention billing as a cause
    assert "billing" not in result.lower()


@pytest.mark.asyncio
async def test_check_workflow_run_startup_failure_classified_account_or_runner(
    respx_mock: respx.MockRouter,
) -> None:
    """Run with startup_failure + no executed sibling → account/runner."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/42/jobs"
        )
    ).respond(json={"jobs": []})
    respx_mock.get(
        url=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "workflow_id": 11,
            "status": "completed",
            "conclusion": "startup_failure",
            "head_sha": "abc123",
        }
    )
    # All siblings also startup_failure.
    respx_mock.get(
        url=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs?per_page=30&head_sha=abc123"
        )
    ).respond(
        json={
            "workflow_runs": [
                {
                    "id": 42,
                    "name": "CI",
                    "workflow_id": 11,
                    "status": "completed",
                    "conclusion": "startup_failure",
                    "head_sha": "abc123",
                },
                {
                    "id": 44,
                    "name": "Lint",
                    "workflow_id": 12,
                    "status": "completed",
                    "conclusion": "startup_failure",
                    "head_sha": "abc123",
                },
            ]
        }
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", run_id=42)
    assert "no sibling workflow" in result
    assert "operator-action ticket" in result
    assert "NOT a workflow-file edit" in result
    # Must NOT mention billing as a cause (classification overrides it)
    assert "billing" not in result.lower()


# ---------------------------------------------------------------------------
# check_workflow_run — branch filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_workflow_run_with_branch_filter(
    respx_mock: respx.MockRouter,
) -> None:
    """Branch filter is passed through to the API."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    runs_route = respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs"
        )
    ).respond(
        json={
            "workflow_runs": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "develop",
                    "event": "push",
                }
            ]
        }
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", branch="develop")
    assert "develop" in result
    assert "Recent workflow runs" in result
    # Verify branch param was included in the request URL
    last_url = str(runs_route.calls.last.request.url)
    assert "branch=develop" in last_url


# ---------------------------------------------------------------------------
# fetch_workflow_run_annotations — scope check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_annotations_refuses_out_of_scope_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → refused message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/allowed-repo"}]})

    tools = build_github_actions_tools(_actions_settings(), dr)
    fetch_annotations = tools[3]

    result = await fetch_annotations("other-repo", 42)
    assert "not installed" in result.lower()
    assert "other-repo" in result


# ---------------------------------------------------------------------------
# fetch_workflow_run_annotations — no check suite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_annotations_no_check_suite(
    respx_mock: respx.MockRouter,
) -> None:
    """Workflow run with no check_suite_id → descriptive message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "check_suite_id": None,
        }
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    fetch_annotations = tools[3]

    result = await fetch_annotations("test-repo", 42)
    assert "no associated check suite" in result.lower()


# ---------------------------------------------------------------------------
# fetch_workflow_run_annotations — no check runs in suite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_annotations_no_check_runs(
    respx_mock: respx.MockRouter,
) -> None:
    """Check suite with no check runs → descriptive message."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "check_suite_id": 500,
        }
    )
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-suites/500/check-runs"
        )
    ).respond(json={"check_runs": []})

    tools = build_github_actions_tools(_actions_settings(), dr)
    fetch_annotations = tools[3]

    result = await fetch_annotations("test-repo", 42)
    assert "no check runs" in result.lower()


# ---------------------------------------------------------------------------
# fetch_workflow_run_annotations — check runs with zero annotations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_annotations_zero_annotations(
    respx_mock: respx.MockRouter,
) -> None:
    """Check runs exist but all have annotations_count == 0."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "check_suite_id": 500,
        }
    )
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-suites/500/check-runs"
        )
    ).respond(
        json={
            "check_runs": [
                {
                    "id": 700,
                    "name": "lint",
                    "conclusion": "failure",
                    "annotations_count": 0,
                },
                {
                    "id": 701,
                    "name": "test",
                    "conclusion": "success",
                    "annotations_count": 0,
                },
            ]
        }
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    fetch_annotations = tools[3]

    result = await fetch_annotations("test-repo", 42)
    assert "none with annotations" in result.lower()


# ---------------------------------------------------------------------------
# fetch_workflow_run_annotations — successful annotation fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_annotations_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Happy path — fetches and formats annotations from check runs."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "check_suite_id": 500,
        }
    )
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-suites/500/check-runs"
        )
    ).respond(
        json={
            "check_runs": [
                {
                    "id": 700,
                    "name": "lint",
                    "conclusion": "failure",
                    "annotations_count": 1,
                },
                {
                    "id": 701,
                    "name": "test",
                    "conclusion": "failure",
                    "annotations_count": 2,
                },
            ]
        }
    )
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-runs/700/annotations"
        )
    ).respond(
        json=[
            {
                "annotation_level": "failure",
                "path": "src/app.py",
                "start_line": 42,
                "end_line": 42,
                "message": "Missing docstring",
                "title": "pylint",
            }
        ]
    )
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-runs/701/annotations"
        )
    ).respond(
        json=[
            {
                "annotation_level": "warning",
                "path": "tests/test_foo.py",
                "start_line": 10,
                "end_line": 15,
                "message": "Test is flaky",
                "title": "pytest",
            },
            {
                "annotation_level": "failure",
                "path": "tests/test_bar.py",
                "start_line": 8,
                "end_line": 8,
                "message": "AssertionError: expected True, got False",
                "title": "",
            },
        ]
    )

    tools = build_github_actions_tools(_actions_settings(), dr)
    fetch_annotations = tools[3]

    result = await fetch_annotations("test-repo", 42)

    # Verify key content
    assert "Workflow run 42 annotations" in result
    assert "3 annotation(s)" in result
    assert "2 check run(s)" in result
    assert "lint" in result.lower()
    assert "test" in result.lower()
    assert "Missing docstring" in result
    assert "Test is flaky" in result
    assert "AssertionError" in result
    assert "src/app.py:42" in result
    assert "tests/test_foo.py:10-15" in result
    assert "pylint" in result
    assert "pytest" in result


# ---------------------------------------------------------------------------
# fetch_workflow_run_annotations — fallback to job logs on Checks API 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_annotations_fallback_on_checks_403(
    respx_mock: respx.MockRouter,
) -> None:
    """When Checks API returns 403, fall back to raw job logs."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    # Fallback: list jobs — register BEFORE the workflow run route so the
    # more-specific /actions/runs/42/jobs pattern matches first.
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/42/jobs"
        )
    ).respond(
        json={
            "jobs": [
                {
                    "id": 100,
                    "name": "quality",
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "id": 101,
                    "name": "deploy",
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
        }
    )
    # Workflow run endpoint succeeds
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "check_suite_id": 500,
        }
    )
    # Check suite listing returns 403
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-suites/500/check-runs"
        )
    ).respond(
        status_code=403,
        json={"message": "Resource not accessible by integration"},
    )
    # Fetch log for failed job
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/jobs/100/logs"
        )
    ).respond(text="Error: missing module 'foo'\nFAILED tests/test_bar.py::test_baz")

    tools = build_github_actions_tools(_actions_settings(), dr)
    fetch_annotations = tools[3]

    result = await fetch_annotations("test-repo", 42)

    # Should contain fallback notice
    assert "403" in result
    assert "checks: read" in result.lower()
    # Should contain the job log content
    assert "Error: missing module" in result
    assert "test_baz" in result
    # Should contain the failed job name
    assert "quality" in result


@pytest.mark.asyncio
async def test_fetch_annotations_fallback_on_check_run_403(
    respx_mock: respx.MockRouter,
) -> None:
    """When individual check-run annotation fetch returns 403, fall back."""
    dr = _direct_repo_settings()

    respx_mock.get(
        url__startswith=f"{dr.github_api_base_url}/installation/repositories"
    ).respond(json={"repositories": [{"full_name": "damien-robotsix/test-repo"}]})
    # Fallback: list jobs — register BEFORE workflow run route.
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/runs/42/jobs"
        )
    ).respond(
        json={
            "jobs": [
                {
                    "id": 700,
                    "name": "quality",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ]
        }
    )
    # Workflow run endpoint succeeds
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo/actions/runs/42"
        )
    ).respond(
        json={
            "id": 42,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "check_suite_id": 500,
        }
    )
    # Check suite listing succeeds
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-suites/500/check-runs"
        )
    ).respond(
        json={
            "check_runs": [
                {
                    "id": 700,
                    "name": "quality",
                    "conclusion": "failure",
                    "annotations_count": 3,
                },
            ]
        }
    )
    # Individual check-run annotation fetch returns 403
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/check-runs/700/annotations"
        )
    ).respond(
        status_code=403,
        json={"message": "Resource not accessible by integration"},
    )
    # Fetch log for failed job
    respx_mock.get(
        url__startswith=(
            f"{dr.github_api_base_url}/repos/damien-robotsix/test-repo"
            "/actions/jobs/700/logs"
        )
    ).respond(text="Traceback (most recent call last):\n  ValueError: bad input")

    tools = build_github_actions_tools(_actions_settings(), dr)
    fetch_annotations = tools[3]

    result = await fetch_annotations("test-repo", 42)

    # Should contain fallback notice
    assert "403" in result
    assert "checks: read" in result.lower()
    # Should contain the job log content
    assert "Traceback" in result
    assert "ValueError" in result
