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


def test_build_github_actions_tools_returns_three_tools() -> None:
    """Enabled github_actions returns three tools.

    set_actions_secret, dispatch_workflow, and check_workflow_run.
    """
    tools = build_github_actions_tools(_actions_settings(), _direct_repo_settings())
    assert len(tools) == 3
    names = {getattr(f, "__name__", str(f)) for f in tools}
    assert names == {"set_actions_secret", "dispatch_workflow", "check_workflow_run"}


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

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo")
    assert "billing" in result.lower()
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
    """Specific run_id with zero jobs → billing diagnostic."""
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

    tools = build_github_actions_tools(_actions_settings(), dr)
    check_run = tools[2]

    result = await check_run("test-repo", run_id=99)
    assert "billing" in result.lower()
    assert "99" in result
    assert "no jobs" in result.lower()


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
