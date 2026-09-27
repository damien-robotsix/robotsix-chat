"""Dedicated unit tests for :mod:`robotsix_chat.repo.direct.github_tools_ci`.

These exercise the CI-monitoring tool closures returned by
:func:`build_github_tools_ci` directly, using a fake :class:`ActionsClient`
(monkeypatched into the module) plus lightweight fakes for the
``DirectRepoClient`` / ``BoardClient``.  The point is to cover the
verdict-classification branches of ``check_ci_health`` and the
run-resolution / error paths of ``rerun_ci_workflow`` and
``fetch_ci_job_logs`` without any network I/O.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from robotsix_chat.repo.direct import github_tools_ci
from robotsix_chat.repo.direct.actions_client import (
    StartupFailureClass,
    StartupFailureClassification,
)
from robotsix_chat.repo.direct.github_tools_ci import build_github_tools_ci


class _FakeClient:
    """Minimal fake ``DirectRepoClient`` returning a canned scope result."""

    def __init__(self, *, scope_error: str | None = None) -> None:
        """Store the canned scope-check result."""
        self._scope_error = scope_error

    async def check_installation_scope(self, repo_full_name: str) -> str | None:
        """Return the configured scope error (or ``None`` to pass)."""
        return self._scope_error


class _FakeBoard:
    """Minimal fake ``BoardClient`` for bare-repo-name resolution."""

    def __init__(self, *, resolved: str | None = None) -> None:
        """Store the canned resolution result."""
        self._resolved = resolved

    async def resolve_repo_full_name(self, name: str) -> str | None:
        """Return the canned resolved ``owner/name`` (or ``None``)."""
        return self._resolved


class _FakeActionsClient:
    """Configurable fake of :class:`ActionsClient` (no network I/O)."""

    def __init__(
        self,
        *,
        runs: list[dict[str, Any]] | None = None,
        default_branch: str = "main",
        classification: StartupFailureClassification | None = None,
        rerun_result: str = "re-run queued",
        run_data: dict[str, Any] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        job_logs: dict[int, str] | None = None,
        list_exc: Exception | None = None,
    ) -> None:
        """Store canned return values / side effects for the fake methods."""
        self._runs = runs or []
        self._default_branch = default_branch
        self._classification = classification
        self._rerun_result = rerun_result
        self._run_data = run_data
        self._jobs = jobs or []
        self._job_logs = job_logs or {}
        self._list_exc = list_exc
        self.reran_run_id: int | None = None
        self.list_calls: list[str] = []

    async def get_default_branch(self, repo_full_name: str) -> str:
        """Return the canned default branch."""
        return self._default_branch

    async def list_workflow_runs(
        self,
        repo_full_name: str,
        *,
        branch: str | None = None,
        head_sha: str | None = None,
        per_page: int = 10,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the canned run list, or raise the configured exception."""
        self.list_calls.append(branch or "")
        if self._list_exc is not None:
            raise self._list_exc
        return self._runs

    async def classify_startup_failure_run(
        self,
        repo_full_name: str,
        failing_run: dict[str, Any],
    ) -> StartupFailureClassification | None:
        """Return the canned startup-failure classification."""
        return self._classification

    async def rerun_workflow_run(self, repo_full_name: str, run_id: int) -> str:
        """Record the re-run id and return the canned status."""
        self.reran_run_id = run_id
        return self._rerun_result

    async def get_workflow_run(
        self, repo_full_name: str, run_id: int
    ) -> dict[str, Any] | None:
        """Return the canned run metadata (or ``None``)."""
        return self._run_data

    async def get_workflow_run_jobs(
        self, repo_full_name: str, run_id: int
    ) -> list[dict[str, Any]]:
        """Return the canned job list."""
        return self._jobs

    async def get_job_log(self, repo_full_name: str, job_id: int) -> str:
        """Return the canned log text for *job_id*."""
        return self._job_logs.get(job_id, "")


async def _fake_resolve_pr_ref(*_a: Any, **_k: Any) -> tuple[str, int] | str:
    """Stand-in ``resolve_pr_ref`` — unused by the tools under test."""
    return "unused"


def _build_ci_tools(
    monkeypatch: pytest.MonkeyPatch,
    actions: _FakeActionsClient,
    *,
    component_request: Any = None,
    client: _FakeClient | None = None,
    board: _FakeBoard | None = None,
) -> dict[str, Any]:
    """Build the CI tools with fakes and return a name -> callable mapping."""
    monkeypatch.setattr(github_tools_ci, "ActionsClient", lambda settings: actions)
    tools = build_github_tools_ci(
        client=cast(Any, client or _FakeClient()),
        board=cast(Any, board or _FakeBoard()),
        settings=cast(Any, object()),
        component_request=component_request,
        resolve_pr_ref=_fake_resolve_pr_ref,
    )
    return {t.__name__: t for t in tools}


def _run(name: str, **kw: Any) -> dict[str, Any]:
    """Build a minimal workflow-run dict."""
    base: dict[str, Any] = {
        "id": 1,
        "name": name,
        "status": "completed",
        "conclusion": "success",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_build_github_tools_ci_returns_six_named_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory returns the six uniquely-named CI tool closures."""
    tools = _build_ci_tools(monkeypatch, _FakeActionsClient())
    assert set(tools) == {
        "check_ci_health",
        "rerun_ci_workflow",
        "fetch_ci_job_logs",
        "fetch_trivy_findings",
        "file_ci_stabilization_ticket",
        "verify_pr_ci_status",
    }


# ---------------------------------------------------------------------------
# check_ci_health — verdict classification branches
# ---------------------------------------------------------------------------


class TestCheckCiHealth:
    """Verdict-classification branches of ``check_ci_health``."""

    @pytest.mark.asyncio
    async def test_missing_repo_full_name_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty repo identifier returns a validation error."""
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient())
        result = await tools["check_ci_health"]()
        assert "repo_full_name is required" in result

    @pytest.mark.asyncio
    async def test_conflicting_repo_aliases_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disagreeing ``repo`` and ``repo_full_name`` are rejected."""
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient())
        result = await tools["check_ci_health"](repo_full_name="org/a", repo="org/b")
        assert "disagree" in result

    @pytest.mark.asyncio
    async def test_scope_error_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing installation-scope check is returned verbatim."""
        client = _FakeClient(scope_error="not installed")
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient(), client=client)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert result == "not installed"

    @pytest.mark.asyncio
    async def test_bare_repo_name_resolved_via_board(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare repo name is resolved through the board roster."""
        actions = _FakeActionsClient(runs=[_run("CI")])
        board = _FakeBoard(resolved="org/resolved")
        tools = _build_ci_tools(monkeypatch, actions, board=board)
        result = await tools["check_ci_health"](repo_full_name="resolved")
        assert "org/resolved" in result

    @pytest.mark.asyncio
    async def test_bare_repo_name_unresolvable_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unresolvable bare repo name returns an error."""
        board = _FakeBoard(resolved=None)
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient(), board=board)
        result = await tools["check_ci_health"](repo_full_name="unknown")
        assert "could not resolve repo" in result

    @pytest.mark.asyncio
    async def test_no_runs_recommends_escalation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty run list yields the escalation recommendation."""
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient(runs=[]))
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "No recent workflow runs found" in result
        assert "escalate" in result

    @pytest.mark.asyncio
    async def test_list_error_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception listing runs is reported, not raised."""
        actions = _FakeActionsClient(list_exc=RuntimeError("boom"))
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "Error checking CI health" in result
        assert "boom" in result

    @pytest.mark.asyncio
    async def test_green_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Latest run success → GREEN verdict."""
        actions = _FakeActionsClient(runs=[_run("CI", conclusion="success")])
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "Verdict: GREEN" in result

    @pytest.mark.asyncio
    async def test_pre_existing_failure_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Latest failure with an earlier green → PRE-EXISTING verdict."""
        actions = _FakeActionsClient(
            runs=[
                _run("CI", id=2, conclusion="failure"),
                _run("CI", id=1, conclusion="success"),
            ]
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "Verdict: PRE-EXISTING failure" in result
        assert "Recommendation: rerun" in result

    @pytest.mark.asyncio
    async def test_failing_no_green_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Latest failure with no green in window → FAILING verdict."""
        actions = _FakeActionsClient(
            runs=[
                _run("CI", id=2, conclusion="failure"),
                _run("CI", id=1, conclusion="cancelled"),
            ]
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "Verdict: FAILING but no green run" in result

    @pytest.mark.asyncio
    async def test_in_progress_no_final_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A still-running latest run yields the no-final-verdict branch."""
        actions = _FakeActionsClient(
            runs=[_run("CI", status="in_progress", conclusion=None)]
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "no final verdict yet" in result

    @pytest.mark.asyncio
    async def test_startup_failure_per_workflow_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A per-workflow-config startup failure blames the workflow file."""
        actions = _FakeActionsClient(
            runs=[_run("CI", conclusion="startup_failure")],
            classification=StartupFailureClassification(
                classification=StartupFailureClass.PER_WORKFLOW_CONFIG,
                summary="one workflow only",
            ),
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "Verdict: STARTUP FAILURE" in result
        assert "one workflow only" in result
        assert "account/runner plane is provably fine" in result

    @pytest.mark.asyncio
    async def test_startup_failure_account_or_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An account/runner startup failure recommends operator action."""
        actions = _FakeActionsClient(
            runs=[_run("CI", conclusion="startup_failure")],
            classification=StartupFailureClassification(
                classification=StartupFailureClass.ACCOUNT_OR_RUNNER,
                summary="all workflows zero jobs",
            ),
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "Verdict: STARTUP FAILURE" in result
        assert "account/runner issue" in result

    @pytest.mark.asyncio
    async def test_startup_failure_without_classification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A startup failure with no classification still returns the verdict."""
        actions = _FakeActionsClient(
            runs=[_run("CI", conclusion="startup_failure")],
            classification=None,
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["check_ci_health"](repo_full_name="org/repo")
        assert "Verdict: STARTUP FAILURE" in result
        assert "Startup-failure classification" not in result


# ---------------------------------------------------------------------------
# rerun_ci_workflow — run resolution & error paths
# ---------------------------------------------------------------------------


class TestRerunCiWorkflow:
    """Run-resolution and error branches of ``rerun_ci_workflow``."""

    @pytest.mark.asyncio
    async def test_scope_error_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing installation-scope check is returned verbatim."""
        client = _FakeClient(scope_error="not installed")
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient(), client=client)
        result = await tools["rerun_ci_workflow"]("org/repo")
        assert result == "not installed"

    @pytest.mark.asyncio
    async def test_explicit_run_id_reruns_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit run_id is re-run without listing runs."""
        actions = _FakeActionsClient(rerun_result="queued 99")
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["rerun_ci_workflow"]("org/repo", run_id=99)
        assert result == "queued 99"
        assert actions.reran_run_id == 99
        assert actions.list_calls == []

    @pytest.mark.asyncio
    async def test_resolves_latest_failed_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no run_id, the most recent failed run is resolved and re-run."""
        actions = _FakeActionsClient(
            runs=[
                _run("CI", id=5, conclusion="failure"),
                _run("CI", id=4, conclusion="success"),
            ]
        )
        tools = _build_ci_tools(monkeypatch, actions)
        await tools["rerun_ci_workflow"]("org/repo")
        assert actions.reran_run_id == 5

    @pytest.mark.asyncio
    async def test_no_failed_run_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No failed run on the branch → nothing-to-re-run message."""
        actions = _FakeActionsClient(runs=[_run("CI", id=4, conclusion="success")])
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["rerun_ci_workflow"]("org/repo", branch="dev")
        assert "No failed workflow run found" in result
        assert "dev" in result
        assert actions.reran_run_id is None

    @pytest.mark.asyncio
    async def test_list_error_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception listing runs is reported, not raised."""
        actions = _FakeActionsClient(list_exc=RuntimeError("boom"))
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["rerun_ci_workflow"]("org/repo")
        assert "Error listing workflow runs" in result
        assert "boom" in result


# ---------------------------------------------------------------------------
# fetch_ci_job_logs — resolution, filtering & log rendering
# ---------------------------------------------------------------------------


class TestFetchCiJobLogs:
    """Resolution, filtering and rendering branches of ``fetch_ci_job_logs``."""

    @pytest.mark.asyncio
    async def test_missing_repo_full_name_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty repo identifier returns a validation error."""
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient())
        result = await tools["fetch_ci_job_logs"]()
        assert "repo_full_name is required" in result

    @pytest.mark.asyncio
    async def test_conflicting_repo_aliases_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disagreeing ``repo`` and ``repo_full_name`` are rejected."""
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient())
        result = await tools["fetch_ci_job_logs"](repo_full_name="org/a", repo="org/b")
        assert "disagree" in result

    @pytest.mark.asyncio
    async def test_no_runs_when_resolving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No runs found while resolving a run_id returns a diagnostic."""
        tools = _build_ci_tools(monkeypatch, _FakeActionsClient(runs=[]))
        result = await tools["fetch_ci_job_logs"](repo_full_name="org/repo")
        assert "No recent workflow runs found" in result

    @pytest.mark.asyncio
    async def test_list_error_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception listing runs is reported, not raised."""
        actions = _FakeActionsClient(list_exc=RuntimeError("boom"))
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](repo_full_name="org/repo")
        assert "could not list workflow runs" in result
        assert "boom" in result

    @pytest.mark.asyncio
    async def test_resolves_latest_failed_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing run is preferred when resolving an unspecified run_id."""
        actions = _FakeActionsClient(
            runs=[
                _run("CI", id=7, conclusion="failure"),
                _run("CI", id=6, conclusion="success"),
            ],
            run_data=_run("CI", id=7, conclusion="failure", head_branch="main"),
            jobs=[{"id": 71, "name": "build", "conclusion": "failure"}],
            job_logs={71: "trace line"},
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](repo_full_name="org/repo")
        assert "Workflow run: 7" in result
        assert "trace line" in result

    @pytest.mark.asyncio
    async def test_run_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing workflow run returns a not-found error."""
        actions = _FakeActionsClient(run_data=None)
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](repo_full_name="org/repo", run_id=42)
        assert "workflow run 42 not found" in result

    @pytest.mark.asyncio
    async def test_no_jobs_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run with no jobs returns the no-jobs message."""
        actions = _FakeActionsClient(
            run_data=_run("CI", id=42, head_branch="main"),
            jobs=[],
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](repo_full_name="org/repo", run_id=42)
        assert "No jobs found for this workflow run" in result

    @pytest.mark.asyncio
    async def test_job_name_filter_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-matching job_name filter lists the available jobs."""
        actions = _FakeActionsClient(
            run_data=_run("CI", id=42, head_branch="main"),
            jobs=[{"id": 1, "name": "build", "conclusion": "success"}],
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](
            repo_full_name="org/repo", run_id=42, job_name="deploy"
        )
        assert "No job named 'deploy' found" in result
        assert "Available jobs: build" in result

    @pytest.mark.asyncio
    async def test_job_name_filter_match_returns_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A matching job_name filter returns only that job's log."""
        actions = _FakeActionsClient(
            run_data=_run("CI", id=42, head_branch="main"),
            jobs=[
                {"id": 1, "name": "build", "conclusion": "success"},
                {"id": 2, "name": "deploy", "conclusion": "failure"},
            ],
            job_logs={1: "build log", 2: "deploy log"},
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](
            repo_full_name="org/repo", run_id=42, job_name="deploy"
        )
        assert "deploy log" in result
        assert "build log" not in result

    @pytest.mark.asyncio
    async def test_log_truncation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A log longer than max_log_bytes is truncated with a note."""
        actions = _FakeActionsClient(
            run_data=_run("CI", id=42, head_branch="main"),
            jobs=[{"id": 1, "name": "build", "conclusion": "failure"}],
            job_logs={1: "x" * 100},
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](
            repo_full_name="org/repo", run_id=42, max_log_bytes=10
        )
        assert "truncated to last 10 bytes" in result

    @pytest.mark.asyncio
    async def test_empty_log_noted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A job with an empty log renders the empty-log marker."""
        actions = _FakeActionsClient(
            run_data=_run("CI", id=42, head_branch="main"),
            jobs=[{"id": 1, "name": "build", "conclusion": "success"}],
            job_logs={1: "   "},
        )
        tools = _build_ci_tools(monkeypatch, actions)
        result = await tools["fetch_ci_job_logs"](repo_full_name="org/repo", run_id=42)
        assert "_(empty log)_" in result
