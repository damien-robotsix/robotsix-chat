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


def test_diagnose_billing_failure_never_started() -> None:
    """Run with no run_started_at → never-started diagnostic."""
    from tests.repo.direct.conftest import _settings

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
    diag = client._diagnose_billing_failure(runs)
    assert diag is None


def test_diagnose_billing_failure_in_progress_skipped() -> None:
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
    diag = client._diagnose_billing_failure(runs)
    assert diag is None


def test_diagnose_billing_failure_empty_runs() -> None:
    """Empty run list → None (no diagnostic)."""
    from tests.repo.direct.conftest import _settings

    client = ActionsClient(_settings())
    diag = client._diagnose_billing_failure([])
    assert diag is None


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


# ---------------------------------------------------------------------------
# Actions variables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_actions_variable_creates(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    ac = ActionsClient(s)

    route = respx_mock.post("https://api.github.com/repos/o/r/actions/variables").mock(
        return_value=httpx.Response(201, text="{}")
    )

    result = await ac.set_actions_variable("o/r", "RELEASE_APP_ID", "3752211")
    assert "created" in result
    body = json.loads(route.calls[0].request.content or "{}")
    assert body == {"name": "RELEASE_APP_ID", "value": "3752211"}


@pytest.mark.asyncio
async def test_set_actions_variable_falls_back_to_patch(
    respx_mock: respx.MockRouter,
) -> None:
    """The create call 409s when the variable exists; the update must run."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    ac = ActionsClient(s)

    respx_mock.post("https://api.github.com/repos/o/r/actions/variables").mock(
        return_value=httpx.Response(409, text="already exists")
    )
    patch_route = respx_mock.patch(
        "https://api.github.com/repos/o/r/actions/variables/RELEASE_APP_ID"
    ).mock(return_value=httpx.Response(204, text=""))

    result = await ac.set_actions_variable("o/r", "RELEASE_APP_ID", "3752211")
    assert "updated" in result
    assert patch_route.called


@pytest.mark.asyncio
async def test_set_actions_variable_reports_error(respx_mock: respx.MockRouter) -> None:
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    ac = ActionsClient(s)

    respx_mock.post("https://api.github.com/repos/o/r/actions/variables").mock(
        return_value=httpx.Response(500, text="boom")
    )
    respx_mock.patch("https://api.github.com/repos/o/r/actions/variables/X").mock(
        return_value=httpx.Response(500, text="boom")
    )

    result = await ac.set_actions_variable("o/r", "X", "v")
    assert "Error setting variable" in result


# ---------------------------------------------------------------------------
# Release-automation bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_wires_variable_secret_and_workflow(
    respx_mock: respx.MockRouter,
) -> None:
    import base64

    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    ac = ActionsClient(s)

    respx_mock.post("https://api.github.com/repos/o/robotsix-x/actions/variables").mock(
        return_value=httpx.Response(201, text="{}")
    )
    respx_mock.get(
        "https://api.github.com/repos/o/robotsix-x/actions/secrets/public-key"
    ).mock(
        return_value=httpx.Response(
            200, text=json.dumps({"key_id": "kid", "key": "cHVibGlj"})
        )
    )
    secret_route = respx_mock.put(
        "https://api.github.com/repos/o/robotsix-x/actions/secrets/RELEASE_APP_PRIVATE_KEY"
    ).mock(return_value=httpx.Response(204, text=""))
    # Workflow absent -> 404 on the read, then created.
    respx_mock.get(
        "https://api.github.com/repos/o/robotsix-x/contents/.github/workflows/auto-release.yml"
    ).mock(return_value=httpx.Response(404, text="Not Found"))
    put_route = respx_mock.put(
        "https://api.github.com/repos/o/robotsix-x/contents/.github/workflows/auto-release.yml"
    ).mock(return_value=httpx.Response(201, text="{}"))

    lines = await ac.bootstrap_release_automation("o/robotsix-x")

    assert any("created" in line for line in lines)
    assert secret_route.called
    assert put_route.called

    body = json.loads(put_route.calls[0].request.content or "{}")
    # Padded base64 — the Contents API rejects the unpadded form the secrets
    # helper uses.
    decoded = base64.b64decode(body["content"]).decode()
    assert "name: Auto Release" in decoded
    assert "vars.RELEASE_APP_ID" in decoded
    assert "app-private-key" in decoded
    # Must pin the App-based revision, not the older release-token one.
    assert "0234f4b82365d776fc021c774dc104c5e7042c29" in decoded
    assert "5fdc956e" not in decoded


@pytest.mark.asyncio
async def test_bootstrap_leaves_an_existing_workflow_alone(
    respx_mock: respx.MockRouter,
) -> None:
    """Never clobber a workflow the repo already has — it may be customised."""
    from tests.repo.direct.conftest import _prepopulate_installation_token, _settings

    s = _settings()
    _prepopulate_installation_token(s)
    ac = ActionsClient(s)

    respx_mock.post("https://api.github.com/repos/o/robotsix-x/actions/variables").mock(
        return_value=httpx.Response(201, text="{}")
    )
    respx_mock.get(
        "https://api.github.com/repos/o/robotsix-x/actions/secrets/public-key"
    ).mock(
        return_value=httpx.Response(
            200, text=json.dumps({"key_id": "kid", "key": "cHVibGlj"})
        )
    )
    respx_mock.put(
        "https://api.github.com/repos/o/robotsix-x/actions/secrets/RELEASE_APP_PRIVATE_KEY"
    ).mock(return_value=httpx.Response(204, text=""))
    respx_mock.get(
        "https://api.github.com/repos/o/robotsix-x/contents/.github/workflows/auto-release.yml"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {"encoding": "base64", "content": "bmFtZTogeA==", "sha": "abc"}
            ),
        )
    )
    put_route = respx_mock.put(
        "https://api.github.com/repos/o/robotsix-x/contents/.github/workflows/auto-release.yml"
    ).mock(return_value=httpx.Response(201, text="{}"))

    lines = await ac.bootstrap_release_automation("o/robotsix-x")
    assert any("already present" in line for line in lines)
    assert not put_route.called
