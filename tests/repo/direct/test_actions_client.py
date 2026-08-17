"""Tests for the GitHub Actions client (:class:`ActionsClient`).

Uses ``respx`` for HTTP mocking — no real network calls.
Shared fixtures live in ``tests/repo/direct/conftest.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from robotsix_chat.repo.direct.actions_client import ActionsClient

# ---------------------------------------------------------------------------
# nacl mock — avoids pynacl import in the sandbox
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_nacl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake ``nacl`` into sys.modules so pynacl is never imported."""
    import sys

    class FakePublicKey:
        def __init__(self, public_key: bytes) -> None:
            self.public_key = public_key

    class FakeSealedBox:
        def __init__(self, public_key: FakePublicKey) -> None:
            self.public_key = public_key

        def encrypt(self, plaintext: bytes) -> bytes:
            return b"encrypted:" + plaintext

    fake_nacl = SimpleNamespace()
    fake_nacl_public = SimpleNamespace()
    fake_nacl_public.PublicKey = FakePublicKey
    fake_nacl_public.SealedBox = FakeSealedBox
    fake_nacl.public = fake_nacl_public

    monkeypatch.setitem(sys.modules, "nacl", fake_nacl)
    monkeypatch.setitem(sys.modules, "nacl.public", fake_nacl_public)


# ---------------------------------------------------------------------------
# dispatch_workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_workflow_success(
    respx_mock: respx.MockRouter,
) -> None:
    """Successful dispatch returns a success message."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.post(
        "https://api.github.com/repos/org/repo/actions/workflows/ci.yml/dispatches"
    ).mock(return_value=httpx.Response(204))

    client = ActionsClient(settings)
    result = await client.dispatch_workflow("org/repo", "ci.yml", "main")
    assert "dispatched successfully" in result
    assert "ci.yml" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_with_inputs(
    respx_mock: respx.MockRouter,
) -> None:
    """dispatch_workflow sends inputs in the JSON body."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    route = respx_mock.post(
        "https://api.github.com/repos/org/repo/actions/workflows/deploy.yml/dispatches"
    ).mock(return_value=httpx.Response(204))

    client = ActionsClient(settings)
    result = await client.dispatch_workflow(
        "org/repo",
        "deploy.yml",
        "main",
        inputs={"environment": "staging", "debug": "true"},
    )
    assert "dispatched successfully" in result

    body = json.loads(route.calls.last.request.content or "{}")
    assert body["ref"] == "main"
    assert body["inputs"] == {"environment": "staging", "debug": "true"}


@pytest.mark.asyncio
async def test_dispatch_workflow_404_not_found(
    respx_mock: respx.MockRouter,
) -> None:
    """404 from GitHub → error message string."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.post(
        "https://api.github.com/repos/org/repo/actions/workflows/missing.yml/dispatches"
    ).mock(return_value=httpx.Response(404, text="Not Found"))

    client = ActionsClient(settings)
    result = await client.dispatch_workflow("org/repo", "missing.yml", "main")
    assert "Error dispatching workflow" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_422_invalid_inputs(
    respx_mock: respx.MockRouter,
) -> None:
    """422 from GitHub → error message string."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.post(
        "https://api.github.com/repos/org/repo/actions/workflows/ci.yml/dispatches"
    ).mock(
        return_value=httpx.Response(
            422,
            text=json.dumps({"message": "Invalid input"}),
        )
    )

    client = ActionsClient(settings)
    result = await client.dispatch_workflow("org/repo", "ci.yml", "main")
    assert "Error dispatching workflow" in result


# ---------------------------------------------------------------------------
# list_workflow_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_workflow_runs_returns_runs(
    respx_mock: respx.MockRouter,
) -> None:
    """list_workflow_runs fetches recent runs and returns them."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

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
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

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
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=10"
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    client = ActionsClient(settings)
    runs = await client.list_workflow_runs("org/repo")
    assert runs == []


@pytest.mark.asyncio
async def test_list_workflow_runs_raise_on_error(
    respx_mock: respx.MockRouter,
) -> None:
    """list_workflow_runs re-raises when raise_on_error=True."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=10"
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    client = ActionsClient(settings)
    with pytest.raises(RuntimeError):
        await client.list_workflow_runs("org/repo", raise_on_error=True)


@pytest.mark.asyncio
async def test_list_workflow_runs_respects_per_page(
    respx_mock: respx.MockRouter,
) -> None:
    """list_workflow_runs clamps per_page to 1–100."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

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
# get_default_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_default_branch_returns_branch(
    respx_mock: respx.MockRouter,
) -> None:
    """get_default_branch reads default_branch from repo metadata."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"default_branch": "develop"}),
        )
    )

    client = ActionsClient(settings)
    assert await client.get_default_branch("org/repo") == "develop"


@pytest.mark.asyncio
async def test_get_default_branch_falls_back_on_error(
    respx_mock: respx.MockRouter,
) -> None:
    """get_default_branch returns 'main' when repo metadata is unavailable."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    client = ActionsClient(settings)
    assert await client.get_default_branch("org/repo") == "main"


# ---------------------------------------------------------------------------
# rerun_workflow_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_workflow_run_success(
    respx_mock: respx.MockRouter,
) -> None:
    """rerun_workflow_run POSTs to the rerun endpoint and reports success."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    route = respx_mock.post(
        "https://api.github.com/repos/org/repo/actions/runs/42/rerun"
    ).mock(return_value=httpx.Response(201, text=""))

    client = ActionsClient(settings)
    result = await client.rerun_workflow_run("org/repo", 42)
    assert "re-run triggered successfully" in result
    assert route.called


@pytest.mark.asyncio
async def test_rerun_workflow_run_error(
    respx_mock: respx.MockRouter,
) -> None:
    """rerun_workflow_run reports an error message on API failure."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.post("https://api.github.com/repos/org/repo/actions/runs/42/rerun").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )

    client = ActionsClient(settings)
    result = await client.rerun_workflow_run("org/repo", 42)
    assert "Error rerunning workflow run 42" in result


# ---------------------------------------------------------------------------
# get_workflow_run_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workflow_run_jobs_returns_jobs(
    respx_mock: respx.MockRouter,
) -> None:
    """get_workflow_run_jobs fetches and returns job list."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

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
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/99/jobs").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    client = ActionsClient(settings)
    jobs = await client.get_workflow_run_jobs("org/repo", 99)
    assert jobs == []


# ---------------------------------------------------------------------------
# get_job_log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_job_log_success(
    respx_mock: respx.MockRouter,
) -> None:
    """get_job_log returns plain-text log on success."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/jobs/100/logs").mock(
        return_value=httpx.Response(200, text="line 1\nline 2\njob done\n")
    )

    client = ActionsClient(settings)
    log = await client.get_job_log("org/repo", 100)
    assert log == "line 1\nline 2\njob done\n"


@pytest.mark.asyncio
async def test_get_job_log_error_raises(
    respx_mock: respx.MockRouter,
) -> None:
    """get_job_log raises RuntimeError when the API returns an error."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/jobs/999/logs").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    client = ActionsClient(settings)
    with pytest.raises(RuntimeError, match="GitHub Actions log"):
        await client.get_job_log("org/repo", 999)


# ---------------------------------------------------------------------------
# set_actions_secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_actions_secret_success(
    respx_mock: respx.MockRouter,
) -> None:
    """set_actions_secret encrypts and sends a secret successfully."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    # Mock public key fetch.
    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/secrets/public-key"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"key_id": "abc123", "key": "ZmFrZS1wdWJsaWMta2V5"}),
        )
    )

    # Mock secret PUT (204 No Content, handled by _request_json).
    respx_mock.put(
        "https://api.github.com/repos/org/repo/actions/secrets/MY_SECRET"
    ).mock(return_value=httpx.Response(204))

    client = ActionsClient(settings)
    result = await client.set_actions_secret("org/repo", "MY_SECRET", "s3cret!")
    assert "set successfully" in result
    assert "MY_SECRET" in result


@pytest.mark.asyncio
async def test_set_actions_secret_missing_pynacl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_actions_secret returns an error when nacl is not importable."""
    import sys

    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    # Remove the fake nacl module injected by _mock_nacl autouse fixture.
    monkeypatch.delitem(sys.modules, "nacl", raising=False)
    monkeypatch.delitem(sys.modules, "nacl.public", raising=False)

    settings = _settings()
    _prepopulate_installation_token(settings)

    client = ActionsClient(settings)
    result = await client.set_actions_secret("org/repo", "S", "v")
    assert "PyNaCl" in result


@pytest.mark.asyncio
async def test_set_actions_secret_key_fetch_error(
    respx_mock: respx.MockRouter,
) -> None:
    """set_actions_secret returns error when public key fetch fails."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/secrets/public-key"
    ).mock(return_value=httpx.Response(500, text="Server Error"))

    client = ActionsClient(settings)
    result = await client.set_actions_secret("org/repo", "S", "v")
    assert "Error fetching repo public key" in result


@pytest.mark.asyncio
async def test_set_actions_secret_api_error(
    respx_mock: respx.MockRouter,
) -> None:
    """set_actions_secret returns error when PUT fails."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    # Mock public key fetch (succeeds).
    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/secrets/public-key"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"key_id": "abc123", "key": "ZmFrZS1wdWJsaWMta2V5"}),
        )
    )

    # Mock secret PUT (fails).
    respx_mock.put("https://api.github.com/repos/org/repo/actions/secrets/SECRET").mock(
        return_value=httpx.Response(422, text="Validation failed")
    )

    client = ActionsClient(settings)
    result = await client.set_actions_secret("org/repo", "SECRET", "v")
    assert "Error setting secret" in result


# ---------------------------------------------------------------------------
# get_workflow_run_annotations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workflow_run_annotations_success(
    respx_mock: respx.MockRouter,
) -> None:
    """get_workflow_run_annotations returns formatted Markdown with annotations."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    # 1. Workflow run → check_suite_id.
    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"check_suite_id": 99}),
        )
    )

    # 2. Check suite → check runs.
    respx_mock.get(
        "https://api.github.com/repos/org/repo/check-suites/99/check-runs?per_page=20&filter=latest"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "check_runs": [
                        {
                            "id": 200,
                            "name": "lint",
                            "conclusion": "failure",
                            "annotations_count": 2,
                        }
                    ]
                }
            ),
        )
    )

    # 3. Check run annotations.
    respx_mock.get(
        "https://api.github.com/repos/org/repo/check-runs/200/annotations?per_page=100"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                [
                    {
                        "annotation_level": "failure",
                        "path": "src/main.py",
                        "start_line": 10,
                        "end_line": 10,
                        "message": "Undefined variable 'x'",
                        "title": "F821",
                    },
                    {
                        "annotation_level": "warning",
                        "path": "src/util.py",
                        "start_line": 5,
                        "end_line": 5,
                        "message": "Unused import os",
                        "title": "F401",
                    },
                ]
            ),
        )
    )

    client = ActionsClient(settings)
    result = await client.get_workflow_run_annotations("org/repo", 42)
    assert "Workflow run 42 annotations" in result
    assert "2 annotation(s)" in result
    assert "Undefined variable" in result
    assert "Unused import" in result


@pytest.mark.asyncio
async def test_get_workflow_run_annotations_no_check_suite(
    respx_mock: respx.MockRouter,
) -> None:
    """get_workflow_run_annotations returns diagnostic when check_suite_id is None."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"check_suite_id": None}),
        )
    )

    client = ActionsClient(settings)
    result = await client.get_workflow_run_annotations("org/repo", 42)
    assert "no associated check suite" in result.lower()


@pytest.mark.asyncio
async def test_get_workflow_run_annotations_no_check_runs(
    respx_mock: respx.MockRouter,
) -> None:
    """get_workflow_run_annotations returns diagnostic when check suite has no runs."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"check_suite_id": 99}),
        )
    )

    respx_mock.get(
        "https://api.github.com/repos/org/repo/check-suites/99/check-runs?per_page=20&filter=latest"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"check_runs": []}),
        )
    )

    client = ActionsClient(settings)
    result = await client.get_workflow_run_annotations("org/repo", 42)
    assert "no check runs" in result.lower()


@pytest.mark.asyncio
async def test_get_workflow_run_annotations_api_error(
    respx_mock: respx.MockRouter,
) -> None:
    """get_workflow_run_annotations returns error message on API failure."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    client = ActionsClient(settings)
    result = await client.get_workflow_run_annotations("org/repo", 42)
    # After the fallback change, API errors trigger a fallback to job logs.
    # When jobs listing also fails (unmocked), we get a clear limitation message.
    assert "unable to diagnose" in result.lower()
    assert "suggestion" in result.lower()


# ---------------------------------------------------------------------------
# _diagnose_billing_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnose_billing_failure_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run with no run_started_at → never-started diagnostic (private repo)."""
    from unittest.mock import AsyncMock

    from tests.repo.direct.conftest import _settings

    client = ActionsClient(_settings())

    # Mock visibility check → private repo
    monkeypatch.setattr(client, "check_repo_visibility", AsyncMock(return_value=True))

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
async def test_diagnose_billing_failure_never_started_public_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run with no run_started_at on a public repo → no billing mention."""
    from unittest.mock import AsyncMock

    from tests.repo.direct.conftest import _settings

    client = ActionsClient(_settings())

    # Mock visibility check → public repo
    monkeypatch.setattr(client, "check_repo_visibility", AsyncMock(return_value=False))

    runs: list[dict[str, object]] = [
        {
            "id": 7,
            "name": "Deploy",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "run_started_at": None,
        }
    ]
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is not None
    assert "public" in diag.lower()
    assert "billing is not the issue" in diag.lower()
    assert "reusable workflow" in diag.lower()
    assert "never started" in diag.lower()


@pytest.mark.asyncio
async def test_diagnose_billing_failure_no_match() -> None:
    """Successful runs → no billing diagnostic."""
    from tests.repo.direct.conftest import _settings

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
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is None


@pytest.mark.asyncio
async def test_diagnose_billing_failure_in_progress_skipped() -> None:
    """In-progress runs are not misdiagnosed as billing failures."""
    from tests.repo.direct.conftest import _settings

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
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is None


@pytest.mark.asyncio
async def test_diagnose_billing_failure_empty_runs() -> None:
    """Empty run list → None (no diagnostic)."""
    from tests.repo.direct.conftest import _settings

    client = ActionsClient(_settings())
    diag = await client._diagnose_billing_failure([], "org/repo")
    assert diag is None


@pytest.mark.asyncio
async def test_diagnose_billing_failure_trigger_config_cross_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-started run with successful sibling → trigger config, not billing."""
    from unittest.mock import AsyncMock

    from tests.repo.direct.conftest import _settings

    client = ActionsClient(_settings())

    # Mock visibility check — _diagnose_billing_failure calls it before
    # the other_succeeded branch, so it must be mocked to avoid a real
    # (unmocked) HTTP request.
    monkeypatch.setattr(client, "check_repo_visibility", AsyncMock(return_value=None))

    # Mock the cross-check to return True (other workflows succeeded)
    mock_cross_check = AsyncMock(return_value=True)
    monkeypatch.setattr(
        client,
        "_other_workflows_succeeded_on_commit",
        mock_cross_check,
    )

    runs: list[dict[str, object]] = [
        {
            "id": 5,
            "name": "Deploy",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "feature/x",
            "head_sha": "abc123",
            "run_started_at": None,
        }
    ]
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is not None
    assert "rules out a billing issue" in diag.lower()
    assert "trigger" in diag.lower()
    # Must NOT mention billing as the cause
    assert "no github actions billing" not in diag.lower()
    # Verify the cross-check was called with the right args
    mock_cross_check.assert_awaited_once_with("org/repo", "abc123", exclude_run_id=5)


@pytest.mark.asyncio
async def test_diagnose_billing_failure_never_started_visibility_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-started run with visibility check failing → neutral fallback."""
    from unittest.mock import AsyncMock

    from tests.repo.direct.conftest import _settings

    client = ActionsClient(_settings())

    # Mock visibility check → None (API error)
    monkeypatch.setattr(client, "check_repo_visibility", AsyncMock(return_value=None))

    runs: list[dict[str, object]] = [
        {
            "id": 9,
            "name": "Deploy",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "run_started_at": None,
        }
    ]
    diag = await client._diagnose_billing_failure(runs, "org/repo")
    assert diag is not None
    # Must NOT claim repo is public
    assert "public repository" not in diag.lower()
    assert "billing is not the issue" not in diag.lower()
    # Neutral fallback: mentions billing as a possibility
    assert "billing" in diag.lower()
    assert "never started" in diag.lower()


# ---------------------------------------------------------------------------
# _classify_startup_failure (pure helper)
# ---------------------------------------------------------------------------


def test_classify_startup_failure_per_workflow_config() -> None:
    """A sibling on the same commit that ran real jobs → per-workflow config."""
    from robotsix_chat.repo.direct.actions_client import (
        StartupFailureClass,
        _classify_startup_failure,
    )

    failing = {
        "id": 1,
        "name": "CI",
        "head_sha": "abc123",
        "conclusion": "startup_failure",
    }
    latest_by_wf = {
        11: {
            "id": 1,
            "workflow_id": 11,
            "name": "CI",
            "head_sha": "abc123",
            "conclusion": "startup_failure",
        },
        12: {
            "id": 2,
            "workflow_id": 12,
            "name": "Lint",
            "head_sha": "abc123",
            "conclusion": "success",
        },
        13: {
            "id": 3,
            "workflow_id": 13,
            "name": "Docs",
            "head_sha": "abc123",
            "conclusion": "failure",
        },
        14: {
            "id": 4,
            "workflow_id": 14,
            "name": "Other",
            "head_sha": "def456",
            "conclusion": "success",
        },
        15: {
            "id": 5,
            "workflow_id": 15,
            "name": "Pending",
            "head_sha": "abc123",
            "conclusion": None,
        },
        16: {
            "id": 6,
            "workflow_id": 16,
            "name": "Cancelled",
            "head_sha": "abc123",
            "conclusion": "cancelled",
        },
        17: {
            "id": 7,
            "workflow_id": 17,
            "name": "AlsoDead",
            "head_sha": "abc123",
            "conclusion": "startup_failure",
        },
    }

    result = _classify_startup_failure(failing, latest_by_wf)
    assert result.classification is StartupFailureClass.PER_WORKFLOW_CONFIG
    assert "2 sibling workflow(s) ran jobs on abc123" in result.summary
    assert "Lint, Docs" in result.summary
    assert "not an account-level problem" in result.summary


def test_classify_startup_failure_account_or_runner_no_executed_siblings() -> None:
    """Every sibling on the commit is startup_failure/zero-job → account/runner."""
    from robotsix_chat.repo.direct.actions_client import (
        StartupFailureClass,
        _classify_startup_failure,
    )

    failing = {
        "id": 1,
        "name": "CI",
        "head_sha": "abc123",
        "conclusion": "startup_failure",
    }
    latest_by_wf = {
        11: {
            "id": 1,
            "workflow_id": 11,
            "name": "CI",
            "head_sha": "abc123",
            "conclusion": "startup_failure",
        },
        12: {
            "id": 2,
            "workflow_id": 12,
            "name": "Lint",
            "head_sha": "abc123",
            "conclusion": "startup_failure",
        },
        13: {
            "id": 3,
            "workflow_id": 13,
            "name": "Docs",
            "head_sha": "abc123",
            "conclusion": "cancelled",
        },
        14: {
            "id": 4,
            "workflow_id": 14,
            "name": "Wait",
            "head_sha": "abc123",
            "conclusion": None,
        },
    }

    result = _classify_startup_failure(failing, latest_by_wf)
    assert result.classification is StartupFailureClass.ACCOUNT_OR_RUNNER
    assert "no sibling workflow on abc123 reached job execution" in result.summary
    assert "operator action" in result.summary


def test_classify_startup_failure_account_or_runner_empty_mapping() -> None:
    """Empty latest_by_wf (no siblings at all) → account/runner."""
    from robotsix_chat.repo.direct.actions_client import (
        StartupFailureClass,
        _classify_startup_failure,
    )

    failing = {"id": 1, "head_sha": "abc123", "conclusion": "startup_failure"}
    result = _classify_startup_failure(failing, {})
    assert result.classification is StartupFailureClass.ACCOUNT_OR_RUNNER
    assert "no sibling workflow on abc123 reached job execution" in result.summary


def test_classify_startup_failure_timed_out_action_required_are_executed() -> None:
    """'timed_out' and 'action_required' conclusions count as job execution."""
    from robotsix_chat.repo.direct.actions_client import (
        StartupFailureClass,
        _classify_startup_failure,
    )

    failing = {"id": 1, "head_sha": "abc123", "conclusion": "startup_failure"}
    latest_by_wf = {
        12: {
            "id": 2,
            "workflow_id": 12,
            "name": "Slow",
            "head_sha": "abc123",
            "conclusion": "timed_out",
        },
        13: {
            "id": 3,
            "workflow_id": 13,
            "name": "NeedsAction",
            "head_sha": "abc123",
            "conclusion": "action_required",
        },
    }

    result = _classify_startup_failure(failing, latest_by_wf)
    assert result.classification is StartupFailureClass.PER_WORKFLOW_CONFIG
    assert "2 sibling workflow(s)" in result.summary


# ---------------------------------------------------------------------------
# check_latest_run_for_zero_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_latest_run_for_zero_jobs_public_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """Zero-job run on a public repo → no billing mention."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    client = ActionsClient(settings)

    # Mock list_workflow_runs to return a single run.
    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=1&branch=main"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 42,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                }
            ),
        )
    )

    # Mock get_workflow_run_jobs to return zero jobs.
    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42/jobs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"jobs": []}),
        )
    )

    # Mock repo visibility → public.
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"private": False}),
        )
    )

    diag = await client.check_latest_run_for_zero_jobs("org/repo", "main")
    assert diag is not None
    assert "public" in diag.lower()
    assert "billing is not the issue" in diag.lower()
    assert "reusable workflow" in diag.lower()
    assert "PRs on this branch are not receiving CI coverage" in diag


@pytest.mark.asyncio
async def test_check_latest_run_for_zero_jobs_visibility_none(
    respx_mock: respx.MockRouter,
) -> None:
    """Zero-job run with visibility check returning None → neutral fallback."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    client = ActionsClient(settings)

    # Mock list_workflow_runs to return a single run.
    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=1&branch=main"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 42,
                            "name": "CI",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                }
            ),
        )
    )

    # Mock get_workflow_run_jobs to return zero jobs.
    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42/jobs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"jobs": []}),
        )
    )

    # Mock repo visibility → error (returns None).
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    diag = await client.check_latest_run_for_zero_jobs("org/repo", "main")
    assert diag is not None
    # Must NOT claim repo is public
    assert "public repository" not in diag.lower()
    assert "billing is not the issue" not in diag.lower()
    # Must mention possible billing (neutral fallback)
    assert "billing" in diag.lower()
    assert "PRs on this branch are not receiving CI coverage" in diag


@pytest.mark.asyncio
async def test_check_latest_run_for_zero_jobs_classified_per_workflow_config(
    respx_mock: respx.MockRouter,
) -> None:
    """Zero-job run with executed sibling on same commit → config, not billing."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    client = ActionsClient(settings)

    # Latest run carries a head_sha and a startup_failure conclusion.
    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=1&branch=main"
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
                        }
                    ]
                }
            ),
        )
    )

    # Zero jobs for the failing run.
    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42/jobs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"jobs": []}),
        )
    )

    # Sibling listing on the same commit: CI failed at startup, Lint ran.
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

    diag = await client.check_latest_run_for_zero_jobs("org/repo", "main")
    assert diag is not None
    assert "per-workflow config" in diag
    assert "1 sibling workflow(s) ran jobs on abc123 (Lint)" in diag
    assert "not an account-level problem" in diag
    assert "root cause is in this workflow's own file" in diag


@pytest.mark.asyncio
async def test_check_latest_run_for_zero_jobs_classified_account_or_runner(
    respx_mock: respx.MockRouter,
) -> None:
    """Zero-job run with no executed sibling → account/runner, not a file edit."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    client = ActionsClient(settings)

    respx_mock.get(
        "https://api.github.com/repos/org/repo/actions/runs?per_page=1&branch=main"
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
                        }
                    ]
                }
            ),
        )
    )

    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42/jobs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"jobs": []}),
        )
    )

    # Every sibling on the commit also produced zero jobs.
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
                            "id": 44,
                            "name": "Lint",
                            "workflow_id": 12,
                            "status": "completed",
                            "conclusion": "startup_failure",
                            "head_sha": "abc123",
                        },
                    ]
                }
            ),
        )
    )

    diag = await client.check_latest_run_for_zero_jobs("org/repo", "main")
    assert diag is not None
    assert "account/runner" in diag
    assert "operator-action ticket" in diag
    assert "NOT a workflow-file edit" in diag


# ---------------------------------------------------------------------------
# _fallback_job_logs — zero-jobs path visibility awareness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_job_logs_no_jobs_public_repo(
    respx_mock: respx.MockRouter,
) -> None:
    """_fallback_job_logs zero-jobs path on public repo → no billing mention."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    client = ActionsClient(settings)

    # Mock repo visibility → public.
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"private": False}),
        )
    )

    # Mock job listing → empty.
    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42/jobs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"jobs": []}),
        )
    )

    result = await client._fallback_job_logs("org/repo", 42, "test error")
    assert "public" in result.lower()
    assert "billing is not the issue" in result.lower()
    assert "reusable workflow" in result.lower()


@pytest.mark.asyncio
async def test_fallback_job_logs_no_jobs_visibility_none(
    respx_mock: respx.MockRouter,
) -> None:
    """_fallback_job_logs zero-jobs path with visibility None → neutral fallback."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    client = ActionsClient(settings)

    # Mock repo visibility → error (returns None).
    respx_mock.get("https://api.github.com/repos/org/repo").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    # Mock job listing → empty.
    respx_mock.get("https://api.github.com/repos/org/repo/actions/runs/42/jobs").mock(
        return_value=httpx.Response(
            200,
            text=json.dumps({"jobs": []}),
        )
    )

    result = await client._fallback_job_logs("org/repo", 42, "test error")
    # Must NOT claim repo is public.
    assert "public repository" not in result.lower()
    assert "billing is not the issue" not in result.lower()
    # Neutral fallback: mentions billing as a possibility.
    assert "billing" in result.lower()


# ---------------------------------------------------------------------------
# auth failure — expired / bad installation token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_workflow_auth_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """dispatch_workflow returns error message on 401 Unauthorized."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.post(
        "https://api.github.com/repos/org/repo/actions/workflows/ci.yml/dispatches"
    ).mock(return_value=httpx.Response(401, text="Bad credentials"))

    client = ActionsClient(settings)
    result = await client.dispatch_workflow("org/repo", "ci.yml", "main")
    assert "Error dispatching workflow" in result


@pytest.mark.asyncio
async def test_get_job_log_auth_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """get_job_log raises RuntimeError on 401 Unauthorized."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    settings = _settings()
    _prepopulate_installation_token(settings)

    respx_mock.get("https://api.github.com/repos/org/repo/actions/jobs/100/logs").mock(
        return_value=httpx.Response(401, text="Bad credentials")
    )

    client = ActionsClient(settings)
    with pytest.raises(RuntimeError):
        await client.get_job_log("org/repo", 100)
