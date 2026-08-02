"""Tests for list_workflow_runs, get_workflow_run_jobs, _diagnose_billing_failure."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from robotsix_chat.repo.direct.actions_client import ActionsClient

from .conftest import _prepopulate_installation_token, _settings

# ---------------------------------------------------------------------------
# list_workflow_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workflow_runs_returns_runs(
    respx_mock: respx.MockRouter,
) -> None:
    """list_workflow_runs fetches recent runs and returns them."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=10"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                            "head_branch": "main",
                            "event": "push",
                        }
                    ]
                }
            ),
        )
    )

    client = ActionsClient(settings)
    runs = await client.list_workflow_runs("org/repo")
    assert len(runs) == 1
    assert runs[0]["id"] == 1
    assert runs[0]["name"] == "CI"


@pytest.mark.asyncio
async def test_list_workflow_runs_with_branch_filter(
    respx_mock: respx.MockRouter,
) -> None:
    """list_workflow_runs passes branch filter as query param."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    route = respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"workflow_runs": []}),
        )
    )

    client = ActionsClient(settings)
    await client.list_workflow_runs("org/repo", branch="develop")

    last_url = str(route.calls.last.request.url)
    assert "branch=develop" in last_url
    assert "per_page=10" in last_url


@pytest.mark.asyncio
async def test_list_workflow_runs_returns_empty_on_error(
    respx_mock: respx.MockRouter,
) -> None:
    """list_workflow_runs returns [] when the API errors."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=10"
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    client = ActionsClient(settings)
    runs = await client.list_workflow_runs("org/repo")
    assert runs == []


@pytest.mark.asyncio
async def test_list_workflow_runs_respects_per_page(
    respx_mock: respx.MockRouter,
) -> None:
    """list_workflow_runs clamps per_page to 1–100."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    route = respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"workflow_runs": []}),
        )
    )

    client = ActionsClient(settings)
    await client.list_workflow_runs("org/repo", per_page=200)

    last_url = str(route.calls.last.request.url)
    assert "per_page=100" in last_url

    await client.list_workflow_runs("org/repo", per_page=0)
    last_url = str(route.calls.last.request.url)
    assert "per_page=1" in last_url


# ---------------------------------------------------------------------------
# get_workflow_run_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workflow_run_jobs_returns_jobs(
    respx_mock: respx.MockRouter,
) -> None:
    """get_workflow_run_jobs fetches and returns job list."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42/jobs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "jobs": [
                        {
                            "id": 100,
                            "name": "build",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                }
            ),
        )
    )

    client = ActionsClient(settings)
    jobs = await client.get_workflow_run_jobs("org/repo", 42)
    assert len(jobs) == 1
    assert jobs[0]["name"] == "build"


@pytest.mark.asyncio
async def test_get_workflow_run_jobs_returns_empty_on_error(
    respx_mock: respx.MockRouter,
) -> None:
    """get_workflow_run_jobs returns [] when the API errors."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/99/jobs").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    client = ActionsClient(settings)
    jobs = await client.get_workflow_run_jobs("org/repo", 99)
    assert jobs == []


# ---------------------------------------------------------------------------
# _diagnose_billing_failure
# ---------------------------------------------------------------------------


def test_diagnose_billing_failure_never_started() -> None:
    """Run with no run_started_at → never-started diagnostic."""
    client = ActionsClient(_settings())
    runs: list[dict[str, object]] = [
        {
            "id": 2,
            "name": "Deploy",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "run_started_at": None,
        }
    ]
    diag = client._diagnose_billing_failure(runs)
    assert diag is not None
    assert "billing" in diag.lower()
    assert "never started" in diag.lower()


def test_diagnose_billing_failure_no_match() -> None:
    """Successful runs → no billing diagnostic."""
    client = ActionsClient(_settings())
    runs: list[dict[str, object]] = [
        {
            "id": 3,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
        }
    ]
    diag = client._diagnose_billing_failure(runs)
    assert diag is None


def test_diagnose_billing_failure_in_progress_skipped() -> None:
    """In-progress runs are not misdiagnosed as billing failures."""
    client = ActionsClient(_settings())
    runs: list[dict[str, object]] = [
        {
            "id": 4,
            "name": "CI",
            "status": "in_progress",
            "conclusion": None,
            "head_branch": "main",
        }
    ]
    diag = client._diagnose_billing_failure(runs)
    assert diag is None


def test_diagnose_billing_failure_empty_runs() -> None:
    """Empty run list → None (no diagnostic)."""
    client = ActionsClient(_settings())
    diag = client._diagnose_billing_failure([])
    assert diag is None
