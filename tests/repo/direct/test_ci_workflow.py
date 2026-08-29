"""Tests for the direct-repo integration.

:func:`build_direct_repo_tools` and :class:`DirectRepoClient`, with ``respx``
mocked so there are no real network calls.
``robotsix_github_auth.mint_installation_token`` is mocked so the shared
library is never imported.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings
from robotsix_chat.repo.direct import build_direct_repo_tools
from robotsix_chat.repo.direct.client import (
    _INSTALLATION_TOKEN_CACHE,
    DirectRepoClient,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _prepopulate_installation_token(settings: DirectRepoSettings) -> None:
    """Seed the installation token cache so tests bypass the token exchange."""
    _INSTALLATION_TOKEN_CACHE[settings.github_app_installation_id] = (
        "ghs_prepopulated_token"  # pragma: allowlist secret
    )


def _settings(**kw: Any) -> DirectRepoSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "github_app_id": "12345",
        "github_app_private_key": "fake-key",  # pragma: allowlist secret
        "github_app_installation_id": "67890",
        "board_api_base_url": "http://127.0.0.1:8077",
    }
    base.update(kw)
    return DirectRepoSettings(**base)


@pytest.fixture(autouse=True)
def _mock_github_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock mint_installation_token so the shared library is never imported."""
    import sys
    from types import SimpleNamespace

    def _fake_mint(**kw: object) -> object:
        return SimpleNamespace(
            token="ghs_test_installation_token"
        )  # pragma: allowlist secret

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)


# ---------------------------------------------------------------------------
# check_ci_health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_ci_health_flags_pre_existing_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """A failing latest run with an earlier green run is pre-existing."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 2,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                        },
                        {
                            "id": 1,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ]
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "check_ci_health"][0]

    out = await fn(repo_full_name="org/repo")
    assert "PRE-EXISTING failure" in out
    assert "branch 'main'" in out


@pytest.mark.asyncio
async def test_check_ci_health_reports_green(
    respx_mock: respx.MockRouter,
) -> None:
    """A successful latest run reports the branch as green."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 3,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "check_ci_health"][0]

    out = await fn(repo_full_name="org/repo")
    assert "Verdict: GREEN" in out


@pytest.mark.asyncio
async def test_check_ci_health_reports_api_error(
    respx_mock: respx.MockRouter,
) -> None:
    """An Actions API failure is surfaced, not reported as 'no runs'."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "check_ci_health"][0]

    out = await fn(repo_full_name="org/repo")
    assert "Error checking CI health for org/repo" in out
    assert "No recent workflow runs found" not in out


@pytest.mark.asyncio
async def test_check_ci_health_startup_failure_classified_per_workflow_config(
    respx_mock: respx.MockRouter,
) -> None:
    """Startup failure run with a green sibling → per-workflow config verdict."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    # Main listing: latest run is startup_failure.
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs?per_page=20"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
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
                            "id": 1,
                            "name": "CI",
                            "workflow_id": 11,
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ]
                }
            ),
        )
    )
    # Sibling listing on the same commit: Lint ran successfully.
    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=30&head_sha=abc123"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
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
                    ]
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "check_ci_health"][0]

    out = await fn(repo_full_name="org/repo")
    assert "STARTUP FAILURE" in out
    assert "1 sibling workflow(s) ran jobs on abc123 (Lint)" in out
    assert "per-workflow config issue" in out
    assert "not an account-level problem" in out


# ---------------------------------------------------------------------------
# rerun_ci_workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_ci_workflow_reruns_latest_failed(
    respx_mock: respx.MockRouter,
) -> None:
    """The latest failed run is re-run when no run_id is supplied."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 7,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                }
            ),
        )
    )
    route = respx_mock.post(
        "https://api.github.com/repos/org/repo/actions/runs/7/rerun"
    ).mock(return_value=httpx.Response(201, text=""))

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "rerun_ci_workflow"][0]

    out = await fn(repo_full_name="org/repo")
    assert "re-run triggered successfully" in out
    assert route.called


@pytest.mark.asyncio
async def test_rerun_ci_workflow_no_failed_run(
    respx_mock: respx.MockRouter,
) -> None:
    """No failed run on the branch produces a clear no-op message."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(return_value=httpx.Response(200, text=json.dumps({"workflow_runs": []})))

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "rerun_ci_workflow"][0]

    out = await fn(repo_full_name="org/repo")
    assert "No failed workflow run found" in out


@pytest.mark.asyncio
async def test_rerun_ci_workflow_reports_api_error(
    respx_mock: respx.MockRouter,
) -> None:
    """An Actions API failure while listing runs is surfaced as an error."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.get(
        url__startswith="https://api.github.com/repos/org/repo/actions/runs"
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "rerun_ci_workflow"][0]

    out = await fn(repo_full_name="org/repo")
    assert "Error listing workflow runs for org/repo" in out
    assert "No failed workflow run found" not in out


# ---------------------------------------------------------------------------
# file_ci_stabilization_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_ci_stabilization_ticket_creates_ticket(
    respx_mock: respx.MockRouter,
) -> None:
    """A CI stabilization escalation files a board ticket and returns its id."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(200, text=json.dumps({"default_branch": "main"}))
    )
    respx_mock.post("http://127.0.0.1:8077/tickets").mock(
        return_value=httpx.Response(
            201,
            text=json.dumps(
                {"id": "t-ci", "title": "CI stabilization", "state": "draft"}
            ),
        )
    )
    respx_mock.get("http://127.0.0.1:8077/tickets/t-ci").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"id": "t-ci", "title": "CI stabilization", "state": "draft"}
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "file_ci_stabilization_ticket"][0]

    out = await fn(repo_full_name="org/repo", summary="Flaky e2e step")
    assert "t-ci" in out
    assert "CI stabilization needed" in out


# ---------------------------------------------------------------------------
# merge_direct_repo_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pr_success(
    respx_mock: respx.MockRouter,
) -> None:
    """mergeable=True, not draft → merge succeeds."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/10").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Fix the thing",
                    "html_url": "https://github.com/org/repo/pull/10",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/10/merge").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "sha": "abc123def456",  # pragma: allowlist secret
                    "merged": True,
                    "message": "Pull Request successfully merged",
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=10,
        pr_title="Fix the thing",
        head_base_branches="fix/t-10 → main",
        merge_method="squash",
    )
    assert "merged successfully" in out.lower()
    assert "abc123def456" in out  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_merge_pr_draft_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """PR is draft → merge refused with diagnostic."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/11").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "WIP thing",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": True,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=11,
        pr_title="WIP thing",
        head_base_branches="fix/t-11 → main",
    )
    assert "draft" in out.lower()
    assert "11" in out


@pytest.mark.asyncio
async def test_merge_pr_conflict_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """PR has merge conflicts → merge refused."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/12").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Conflicting PR",
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=12,
        pr_title="Conflicting PR",
        head_base_branches="fix/t-12 → main",
    )
    assert "merge conflicts" in out.lower()
    assert "12" in out


@pytest.mark.asyncio
async def test_merge_pr_unknown_mergeability_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """mergeable=None → merge refused (still computing)."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/13").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Pending PR",
                    "mergeable": None,
                    "mergeable_state": "unknown",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=13,
        pr_title="Pending PR",
        head_base_branches="fix/t-13 → main",
    )
    assert "still being computed" in out.lower()


@pytest.mark.asyncio
async def test_merge_pr_already_merged(
    respx_mock: respx.MockRouter,
) -> None:
    """PR already merged → informative message."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/14").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Already merged",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": True,
                    "merge_commit_sha": "sha123",
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=14,
        pr_title="Already merged",
        head_base_branches="fix/t-14 → main",
    )
    assert "already merged" in out.lower()


@pytest.mark.asyncio
async def test_merge_pr_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in installation scope → merge refused."""
    settings = _settings()

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other"}]}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=1,
        pr_title="Any",
        head_base_branches="fix → main",
    )
    assert "not installed" in out.lower()
    assert "install" in out.lower()


@pytest.mark.asyncio
async def test_merge_pr_github_405(
    respx_mock: respx.MockRouter,
) -> None:
    """GitHub returns 405 (not mergeable) → diagnostic message."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/15").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "CI failing",
                    "mergeable": True,
                    "mergeable_state": "blocked",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/15/merge").mock(
        return_value=httpx.Response(
            405,
            text=json.dumps({"message": "Pull Request is not mergeable"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "merge_direct_repo_pr"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=15,
        pr_title="CI failing",
        head_base_branches="fix/t-15 → main",
    )
    assert "not in a mergeable state" in out.lower()
    assert "status checks" in out.lower()


# ---------------------------------------------------------------------------
# arm_direct_repo_auto_merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_auto_merge_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Not draft, not merged → auto-merge enabled."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/20").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Ready PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/20/auto-merge").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"message": "Auto-merge enabled", "merge_method": "squash"}
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=20,
        pr_title="Ready PR",
        head_base_branches="fix/t-20 → main",
        merge_method="squash",
    )
    assert "auto-merge enabled" in out.lower()
    assert "20" in out


@pytest.mark.asyncio
async def test_arm_auto_merge_draft_refused(
    respx_mock: respx.MockRouter,
) -> None:
    """PR is draft → auto-merge refused."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/21").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Draft PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": True,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=21,
        pr_title="Draft PR",
        head_base_branches="fix/t-21 → main",
    )
    assert "draft" in out.lower()


@pytest.mark.asyncio
async def test_arm_auto_merge_already_merged(
    respx_mock: respx.MockRouter,
) -> None:
    """PR already merged → auto-merge refused."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/22").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "Merged PR",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": True,
                    "draft": False,
                }
            ),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=22,
        pr_title="Merged PR",
        head_base_branches="fix/t-22 → main",
    )
    assert "already merged" in out.lower()


@pytest.mark.asyncio
async def test_arm_auto_merge_out_of_scope(
    respx_mock: respx.MockRouter,
) -> None:
    """Repo not in scope → auto-merge refused."""
    settings = _settings()

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/other"}]}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=1,
        pr_title="Any",
        head_base_branches="fix → main",
    )
    assert "not installed" in out.lower()
    assert "install" in out.lower()


@pytest.mark.asyncio
async def test_arm_auto_merge_github_error(
    respx_mock: respx.MockRouter,
) -> None:
    """GitHub returns 403/404 (auto-merge not available) → diagnostic."""
    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"repositories": [{"full_name": "org/repo"}]}),
        )
    )
    respx_mock.get("https://api.github.com/repos/org/repo/pulls/23").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "title": "No auto-merge repo",
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "merged": False,
                    "draft": False,
                }
            ),
        )
    )
    respx_mock.put("https://api.github.com/repos/org/repo/pulls/23/auto-merge").mock(
        return_value=httpx.Response(
            403,
            text=json.dumps({"message": "Auto-merge is not allowed"}),
        )
    )

    tools = build_direct_repo_tools(settings)
    fn = [t for t in tools if t.__name__ == "arm_direct_repo_auto_merge"][0]

    out = await fn(
        repo_full_name="org/repo",
        pr_number=23,
        pr_title="No auto-merge repo",
        head_base_branches="fix/t-23 → main",
    )
    assert "auto-merge" in out.lower()
    assert "23" in out


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

    client = DirectRepoClient(settings)
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

    client = DirectRepoClient(settings)
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

    client = DirectRepoClient(settings)
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

    client = DirectRepoClient(settings)
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

    client = DirectRepoClient(settings)
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

    client = DirectRepoClient(settings)
    jobs = await client.get_workflow_run_jobs("org/repo", 99)
    assert jobs == []


# ---------------------------------------------------------------------------
# _diagnose_billing_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnose_billing_failure_never_started() -> None:
    """Run with no run_started_at → never-started diagnostic."""
    client = DirectRepoClient(_settings())
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
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is not None
    assert "billing" in diag.lower()
    assert "never started" in diag.lower()


@pytest.mark.asyncio
async def test_diagnose_billing_failure_no_match() -> None:
    """Successful runs → no billing diagnostic."""
    client = DirectRepoClient(_settings())
    runs: list[dict[str, object]] = [
        {
            "id": 3,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
        }
    ]
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is None


@pytest.mark.asyncio
async def test_diagnose_billing_failure_in_progress_skipped() -> None:
    """In-progress runs are not misdiagnosed as billing failures."""
    client = DirectRepoClient(_settings())
    runs: list[dict[str, object]] = [
        {
            "id": 4,
            "name": "CI",
            "status": "in_progress",
            "conclusion": None,
            "head_branch": "main",
        }
    ]
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is None


@pytest.mark.asyncio
async def test_diagnose_billing_failure_empty_runs() -> None:
    """Empty run list → None (no diagnostic)."""
    client = DirectRepoClient(_settings())
    diag = await client._diagnose_billing_failure([], "org/repo")
    assert diag is None
