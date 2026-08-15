"""Tests for ``src/robotsix_mill/stages/document.py``.

Covers ``_run_mdformat_on_changed_md_files`` (module-level function)
and ``DocumentStage.run`` (the stage's main entry point).

Uses an importlib-based fake-module harness to load ``document.py``
directly from the source file, because the ``robotsix_mill`` shadow
package's sibling modules (core, agents, config, notify, vcs) do not
exist in this checkout.
"""

from __future__ import annotations

import enum
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load document.py via importlib — stub out missing sibling modules first
# ---------------------------------------------------------------------------

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "robotsix_mill"
    / "stages"
    / "document.py"
)


class _FakeState(enum.StrEnum):
    """Minimal State enum matching the values used in document.py."""

    DOCUMENTING = "DOCUMENTING"
    DELIVERABLE = "DELIVERABLE"
    BLOCKED = "BLOCKED"
    ERRORED = "ERRORED"


class _FakeOutcome(SimpleNamespace):
    """A fake Outcome that behaves like the real namedtuple/simple-class."""

    def __init__(self, state: _FakeState, note: str = "") -> None:
        super().__init__(state=state, note=note)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _FakeOutcome):
            return self.state == other.state and self.note == other.note
        return NotImplemented

    def __repr__(self) -> str:
        return f"Outcome(state={self.state!r}, note={self.note!r})"


class _FakeStage:
    """Minimal base Stage so DocumentStage(Stage) doesn't crash."""

    name: str = ""
    input_state: str = ""
    traced: bool = False


class _FakeStageContext(SimpleNamespace):
    """Minimal StageContext for use in tests."""

    def __init__(
        self,
        settings: Any = None,
        service: Any = None,
        repo_config: Any = None,
    ) -> None:
        super().__init__(
            settings=settings,
            service=service,
            repo_config=repo_config,
        )


class _FakeDocClassifierResult(SimpleNamespace):
    def __init__(self, user_facing: bool, classification: str = "") -> None:
        super().__init__(user_facing=user_facing, classification=classification)


class _FakeDocResult(SimpleNamespace):
    def __init__(
        self,
        user_facing: bool,
        summary: str = "",
        edited_files: list[str] | None = None,
    ) -> None:
        super().__init__(user_facing=user_facing, summary=summary)


class UsageLimitExceededError(RuntimeError):
    """Fake pydantic-ai usage-cap exception for retry tests."""


# --- Stub all missing sibling modules in sys.modules ---

_fake_core_states = SimpleNamespace(State=_FakeState)
_fake_stages_base = SimpleNamespace(
    Outcome=_FakeOutcome,
    Stage=_FakeStage,
    StageContext=_FakeStageContext,
)
_fake_agents_documenting = SimpleNamespace(
    DocClassifierResult=_FakeDocClassifierResult,
    DocResult=_FakeDocResult,
    run_doc_agent=MagicMock(),
    run_doc_classifier=MagicMock(),
)
_fake_config = SimpleNamespace(target_branch_for=MagicMock(return_value="main"))
_fake_core_models = SimpleNamespace(Ticket=MagicMock)
_fake_notify = SimpleNamespace(send_notification=MagicMock())
_fake_vcs_git_ops = SimpleNamespace(
    _paths_from_diff=MagicMock(return_value=set()),
    has_changes=MagicMock(return_value=False),
    commit_all=MagicMock(),
    redact_credentials=MagicMock(side_effect=lambda x: x),
)
_fake_implemented_repos = SimpleNamespace(
    combined_diff=MagicMock(return_value="fake diff"),
    implemented_repos=MagicMock(return_value=[]),
)


def _make_pkg_stub(name: str) -> Any:
    """Create a mock module that satisfies package-resolution imports.

    The returned module looks enough like a package to satisfy the import
    system during ``from ..pkg import submodule`` resolution.
    """
    import types

    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__package__ = name
    return mod


_stubs: dict[str, Any] = {
    "robotsix_mill": _make_pkg_stub("robotsix_mill"),
    "robotsix_mill.core": _make_pkg_stub("robotsix_mill.core"),
    "robotsix_mill.core.states": _fake_core_states,
    "robotsix_mill.core.models": _fake_core_models,
    "robotsix_mill.stages": _make_pkg_stub("robotsix_mill.stages"),
    "robotsix_mill.stages.base": _fake_stages_base,
    "robotsix_mill.stages._implemented_repos": _fake_implemented_repos,
    "robotsix_mill.agents": _make_pkg_stub("robotsix_mill.agents"),
    "robotsix_mill.agents.documenting": _fake_agents_documenting,
    "robotsix_mill.config": _fake_config,
    "robotsix_mill.notify": _fake_notify,
    "robotsix_mill.vcs": _make_pkg_stub("robotsix_mill.vcs"),
    "robotsix_mill.vcs.git_ops": _fake_vcs_git_ops,
    "robotsix_mill.runtime": _make_pkg_stub("robotsix_mill.runtime"),
    "robotsix_mill.runtime.transient_errors": SimpleNamespace(
        reraise_if_transient=MagicMock(),
    ),
}

for _mod_name, _stub in _stubs.items():
    sys.modules[_mod_name] = _stub

_spec = importlib.util.spec_from_file_location("robotsix_mill.stages.document", _SOURCE)
assert _spec is not None, f"Could not load spec for {_SOURCE}"
_document = importlib.util.module_from_spec(_spec)
# Ensure __package__ is set so that relative imports inside except
# blocks (e.g. ``from ..runtime.transient_errors import …``) resolve
# correctly at runtime.
_document.__package__ = "robotsix_mill.stages"
# Also register the document module itself so that runtime ``from ..x``
# imports inside method bodies can traverse the package hierarchy.
sys.modules["robotsix_mill.stages.document"] = _document
assert _spec.loader is not None
_spec.loader.exec_module(_document)

_run_mdformat_on_changed_md_files = _document._run_mdformat_on_changed_md_files
DocumentStage = _document.DocumentStage
_FakeOutcomeCls = _FakeOutcome  # local alias for creating outcomes
_FakeStateEnum = _FakeState
_FakeStageContextCls = _FakeStageContext

# ---------------------------------------------------------------------------
# _run_mdformat_on_changed_md_files
# ---------------------------------------------------------------------------


class TestRunMdformatOnChangedMdFiles:
    """Tests for ``_run_mdformat_on_changed_md_files``."""

    def test_empty_diff_returns_early(self, tmp_path: Path) -> None:
        """No changed .md files — function returns without running mdformat."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            _run_mdformat_on_changed_md_files(tmp_path)
            # Only the git diff call was made
            assert mock_run.call_count == 1
            assert mock_run.call_args[0][0][:3] == ["git", "diff", "--name-only"]

    def test_excluded_md_files_skipped(self, tmp_path: Path) -> None:
        """CHANGELOG.md and changelog.d/* files are excluded from formatting."""
        with patch("subprocess.run") as mock_run:
            # First call: git diff returns excluded .md files
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="CHANGELOG.md\nchangelog.d/123.misc.md\n",
                stderr="",
            )
            mock_run.side_effect = [git_result]
            _run_mdformat_on_changed_md_files(tmp_path)
            # Only git diff was called (no mdformat call because all files excluded)
            assert mock_run.call_count == 1

    def test_non_md_files_skipped(self, tmp_path: Path) -> None:
        """Only .md files are considered; other extensions are ignored."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="src/foo.py\nsrc/bar.rs\ndocs/index.html\n",
                stderr="",
            )
            mock_run.side_effect = [git_result]
            _run_mdformat_on_changed_md_files(tmp_path)
            assert mock_run.call_count == 1  # no mdformat

    def test_mixed_files_only_md_formatted(self, tmp_path: Path) -> None:
        """Among a mix of changed files, only .md files are passed to mdformat."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="README.md\nsrc/foo.py\nAGENT.md\nCHANGELOG.md\n",
                stderr="",
            )
            # mdformat call succeeds
            mdformat_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_run.side_effect = [git_result, mdformat_result]
            _run_mdformat_on_changed_md_files(tmp_path)
            # Two calls: git diff + mdformat
            assert mock_run.call_count == 2
            mdformat_call = mock_run.call_args_list[1]
            # Only README.md, AGENT.md (CHANGELOG.md excluded)
            md_files = mdformat_call[0][0][3:]  # skip ["uv", "run", "mdformat", ...]
            assert "README.md" in md_files
            assert "AGENT.md" in md_files
            assert "CHANGELOG.md" not in md_files

    def test_git_diff_failure_logs_warning(self, tmp_path: Path) -> None:
        """When git diff fails, function logs a warning and returns."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=128, cmd=["git", "diff", "--name-only"]
            )
            with patch("logging.Logger.warning") as mock_warn:
                _run_mdformat_on_changed_md_files(tmp_path)
                mock_warn.assert_called_once()
                assert "git diff failed" in mock_warn.call_args[0][0]

    def test_uv_run_mdformat_preferred(self, tmp_path: Path) -> None:
        """When uv is available, prefer ``uv run mdformat``."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="README.md\n",
                stderr="",
            )
            mdformat_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_run.side_effect = [git_result, mdformat_result]
            _run_mdformat_on_changed_md_files(tmp_path)
            mdformat_call = mock_run.call_args_list[1]
            assert mdformat_call[0][0][:3] == ["uv", "run", "mdformat"]

    def test_fallback_to_python3_mdformat(self, tmp_path: Path) -> None:
        """When uv run mdformat fails, fall back to python3 -m mdformat."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="README.md\n",
                stderr="",
            )
            # uv fails, python3 succeeds
            uv_fail = subprocess.CalledProcessError(
                returncode=1, cmd=["uv", "run", "mdformat"], stderr="not found"
            )
            py_success = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_run.side_effect = [git_result, uv_fail, py_success]
            _run_mdformat_on_changed_md_files(tmp_path)
            # Three calls: git diff, uv (fails), python3 (succeeds)
            assert mock_run.call_count == 3
            py_call = mock_run.call_args_list[2]
            assert py_call[0][0][:3] == ["python3", "-m", "mdformat"]

    def test_both_runners_fail_logs_debug(self, tmp_path: Path) -> None:
        """When both uv and python3 mdformat fail, log debug and return."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="README.md\n",
                stderr="",
            )
            uv_fail = subprocess.CalledProcessError(
                returncode=1, cmd=["uv", "run", "mdformat"], stderr="no uv"
            )
            py_fail = subprocess.CalledProcessError(
                returncode=1, cmd=["python3", "-m", "mdformat"], stderr="no module"
            )
            mock_run.side_effect = [git_result, uv_fail, py_fail]
            with patch("logging.Logger.debug") as mock_debug:
                _run_mdformat_on_changed_md_files(tmp_path)
                # Last call to debug should be "no available runner"
                debug_calls = [
                    c
                    for c in mock_debug.call_args_list
                    if "no available runner" in str(c)
                ]
                assert len(debug_calls) == 1

    def test_file_not_found_fallback_to_python3(self, tmp_path: Path) -> None:
        """When uv binary is not found, fall back to python3."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="README.md\n",
                stderr="",
            )
            py_success = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_run.side_effect = [git_result, FileNotFoundError("uv"), py_success]
            _run_mdformat_on_changed_md_files(tmp_path)
            assert mock_run.call_count == 3
            py_call = mock_run.call_args_list[2]
            assert py_call[0][0][:3] == ["python3", "-m", "mdformat"]

    def test_mdformat_args_number_and_wrap(self, tmp_path: Path) -> None:
        """Mdformat is called with --number and --wrap 100."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="README.md\n",
                stderr="",
            )
            mdformat_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_run.side_effect = [git_result, mdformat_result]
            _run_mdformat_on_changed_md_files(tmp_path)
            mdformat_call = mock_run.call_args_list[1]
            args = mdformat_call[0][0]
            assert "--number" in args
            assert "--wrap" in args
            assert "100" in args

    def test_blank_lines_in_diff_output_handled(self, tmp_path: Path) -> None:
        """Blank lines in git diff --name-only output are skipped."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="\nREADME.md\n\nAGENT.md\n\n",
                stderr="",
            )
            mdformat_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_run.side_effect = [git_result, mdformat_result]
            _run_mdformat_on_changed_md_files(tmp_path)
            mdformat_call = mock_run.call_args_list[1]
            md_files = mdformat_call[0][0][3:]  # skip uv, run, mdformat
            # md_files = ["--number", "--wrap", "100", "README.md", "AGENT.md"]
            assert "README.md" in md_files
            assert "AGENT.md" in md_files

    def test_repo_dir_passed_as_cwd(self, tmp_path: Path) -> None:
        """The repo_dir is passed as cwd to both git and mdformat calls."""
        with patch("subprocess.run") as mock_run:
            git_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="README.md\n",
                stderr="",
            )
            mdformat_result = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_run.side_effect = [git_result, mdformat_result]
            _run_mdformat_on_changed_md_files(tmp_path)
            # Both calls should use tmp_path as cwd
            for call in mock_run.call_args_list:
                assert call[1]["cwd"] == tmp_path


# ---------------------------------------------------------------------------
# DocumentStage.run
# ---------------------------------------------------------------------------


class _FakeRepoInfo(SimpleNamespace):
    def __init__(self, repo_dir: Path) -> None:
        super().__init__(repo_dir=repo_dir)


class _FakeService(SimpleNamespace):
    """A fake workspace service with configurable add_step_event."""

    def __init__(self) -> None:
        self.step_events: list[tuple[str, str]] = []
        self._workspace = MagicMock()

    def workspace(self, ticket: Any) -> Any:
        return self._workspace

    def add_step_event(self, ticket_id: str, message: str) -> None:
        self.step_events.append((ticket_id, message))


def _make_stage_context(
    tmp_path: Path,
    *,
    repos: list[_FakeRepoInfo] | None = None,
    diff: str = "fake diff",
    settings: Any = None,
    repo_config: Any = None,
) -> _FakeStageContext:
    """Build a ``StageContext`` suitable for testing ``DocumentStage.run``.

    Only creates the service/workspace/context objects and wires
    ``implemented_repos`` — does **not** configure other mock return
    values (the ``_reset_shared_mocks`` fixture and individual tests
    handle those).  The *diff* parameter is purely for the caller's
    reference; tests must set
    ``_fake_implemented_repos.combined_diff.return_value`` explicitly.
    """
    if repos is None:
        repos = [_FakeRepoInfo(tmp_path / "repo")]
    svc = _FakeService()
    ws = svc._workspace
    ws.dir = tmp_path
    ws.read_description = MagicMock(return_value="spec text")

    # Wire implemented_repos so that ``implemented_repos(...)`` returns
    # the test's repo list.
    _fake_implemented_repos.implemented_repos.return_value = repos

    return _FakeStageContextCls(
        settings=settings or SimpleNamespace(),
        service=svc,
        repo_config=repo_config or SimpleNamespace(board_id="test-board"),
    )


@pytest.fixture(autouse=True)
def _reset_shared_mocks() -> None:
    """Reset all shared MagicMock objects between tests to prevent state bleed.

    Without this, a test that sets ``side_effect`` on a shared mock
    contaminates every subsequent test that calls the same mock.
    """
    _fake_implemented_repos.combined_diff.reset_mock()
    _fake_implemented_repos.combined_diff.return_value = "fake diff"
    _fake_implemented_repos.combined_diff.side_effect = None
    _fake_implemented_repos.implemented_repos.reset_mock()
    _fake_implemented_repos.implemented_repos.return_value = []
    _fake_implemented_repos.implemented_repos.side_effect = None
    _fake_agents_documenting.run_doc_agent.reset_mock()
    _fake_agents_documenting.run_doc_agent.side_effect = None
    _fake_agents_documenting.run_doc_classifier.reset_mock()
    _fake_agents_documenting.run_doc_classifier.side_effect = None
    _fake_vcs_git_ops.commit_all.reset_mock()
    _fake_vcs_git_ops.commit_all.side_effect = None
    _fake_vcs_git_ops.has_changes.reset_mock()
    _fake_vcs_git_ops.has_changes.return_value = False
    _fake_vcs_git_ops.has_changes.side_effect = None
    _fake_vcs_git_ops._paths_from_diff.reset_mock()
    _fake_vcs_git_ops._paths_from_diff.return_value = {"README.md", "src/foo.py"}
    _fake_vcs_git_ops._paths_from_diff.side_effect = None
    _fake_vcs_git_ops.redact_credentials.reset_mock()
    _fake_vcs_git_ops.redact_credentials.side_effect = lambda x: x
    _fake_notify.send_notification.reset_mock()
    _fake_notify.send_notification.side_effect = None
    _fake_config.target_branch_for.reset_mock()
    _fake_config.target_branch_for.return_value = "main"
    _fake_config.target_branch_for.side_effect = None


class TestDocumentStageRun:
    """Tests for ``DocumentStage.run``."""

    # ------------------------------------------------------------------
    # No repos
    # ------------------------------------------------------------------

    def test_no_implemented_repos_returns_blocked(self, tmp_path: Path) -> None:
        """When implemented_repos returns empty, stage returns BLOCKED."""
        _fake_implemented_repos.implemented_repos.return_value = []
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path, repos=[])
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.BLOCKED
        assert "no repository clone" in outcome.note

    # ------------------------------------------------------------------
    # Empty diff
    # ------------------------------------------------------------------

    def test_empty_diff_returns_deliverable(self, tmp_path: Path) -> None:
        """Empty diff → DELIVERABLE with a note about no documentation needed."""
        _fake_implemented_repos.combined_diff.return_value = ""
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path, diff="")
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "empty diff" in outcome.note

    def test_whitespace_only_diff_treated_as_empty(self, tmp_path: Path) -> None:
        """A diff that is only whitespace → empty → DELIVERABLE."""
        _fake_implemented_repos.combined_diff.return_value = "   \n  \n"
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path, diff="   \n  \n")
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "empty diff" in outcome.note

    # ------------------------------------------------------------------
    # Doc-only diff (deterministic short-circuit)
    # ------------------------------------------------------------------

    def test_doc_only_diff_skips_agent(self, tmp_path: Path) -> None:
        """When every modified file is .md or under docs/, skip the doc agent."""
        _fake_vcs_git_ops._paths_from_diff.return_value = {
            "README.md",
            "docs/guide.md",
            "CHANGELOG.md",
        }
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "doc-only" in outcome.note.lower()

    def test_doc_only_diff_docs_directory_prefix(self, tmp_path: Path) -> None:
        """Files under docs/ (even without .md extension) trigger the short-circuit."""
        _fake_vcs_git_ops._paths_from_diff.return_value = {
            "docs/conf.py",
            "docs/index.rst",
        }
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "doc-only" in outcome.note.lower()

    def test_mixed_diff_does_not_short_circuit(self, tmp_path: Path) -> None:
        """When diff includes src/ and docs/, do NOT short-circuit."""
        _fake_vcs_git_ops._paths_from_diff.return_value = {
            "src/main.py",
            "docs/guide.md",
        }
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(
                user_facing=False, classification="internal refactor"
            )
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        # Classifier was called (not short-circuited)
        _fake_agents_documenting.run_doc_classifier.assert_called_once()
        assert outcome.state == _FakeStateEnum.DELIVERABLE

    # ------------------------------------------------------------------
    # combined_diff failure → BLOCKED
    # ------------------------------------------------------------------

    def test_combined_diff_failure_returns_blocked(self, tmp_path: Path) -> None:
        """When combined_diff raises, outcome is BLOCKED (non-transient error)."""
        _fake_implemented_repos.combined_diff.side_effect = RuntimeError(
            "git fetch failed"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.BLOCKED
        assert "failed to compute diff" in outcome.note

    # ------------------------------------------------------------------
    # Classifier: internal-only
    # ------------------------------------------------------------------

    def test_classifier_internal_only_returns_deliverable(self, tmp_path: Path) -> None:
        """Classifier returns user_facing=False → DELIVERABLE with classifier note."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(
                user_facing=False, classification="internal refactor"
            )
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "doc_classifier" in outcome.note
        assert "internal refactor" in outcome.note

    def test_classifier_internal_does_not_call_doc_agent(self, tmp_path: Path) -> None:
        """When classifier says internal-only, the full doc agent is not invoked."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(
                user_facing=False, classification="no user-facing changes"
            )
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        stage.run(ticket, ctx)
        _fake_agents_documenting.run_doc_agent.assert_not_called()

    # ------------------------------------------------------------------
    # Classifier: failure → fall through to full agent
    # ------------------------------------------------------------------

    def test_classifier_failure_falls_through_to_doc_agent(
        self, tmp_path: Path
    ) -> None:
        """When the classifier raises, the full doc agent runs instead."""
        _fake_agents_documenting.run_doc_classifier.side_effect = RuntimeError("boom")
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=False, summary="no changes"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        _fake_agents_documenting.run_doc_agent.assert_called_once()
        assert outcome.state == _FakeStateEnum.DELIVERABLE

    # ------------------------------------------------------------------
    # Full doc agent: user_facing=False
    # ------------------------------------------------------------------

    def test_doc_agent_not_user_facing(self, tmp_path: Path) -> None:
        """Doc agent returns user_facing=False → DELIVERABLE."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(
                user_facing=True, classification="user-facing change"
            )
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=False, summary="no user-facing changes (internal-only)"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "internal-only" in outcome.note

    # ------------------------------------------------------------------
    # Full doc agent: user_facing=True, has changes → commit
    # ------------------------------------------------------------------

    def test_doc_agent_user_facing_with_changes(self, tmp_path: Path) -> None:
        """Doc agent reports user-facing, has_changes=True → commit + mdformat."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(
                user_facing=True, classification="user-facing change"
            )
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=True,
            summary="updated README.md with new feature docs",
        )
        _fake_vcs_git_ops.has_changes.return_value = True

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Add new feature"

        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            outcome = stage.run(ticket, ctx)

        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "updated README.md" in outcome.note
        _fake_vcs_git_ops.commit_all.assert_called_once()

    def test_doc_agent_user_facing_calls_mdformat(self, tmp_path: Path) -> None:
        """After a doc commit, mdformat runs on changed .md files."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=True, summary="docs updated"
        )
        _fake_vcs_git_ops.has_changes.return_value = True

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Test"

        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            stage.run(ticket, ctx)

        # We expect at least the git diff call + mdformat call via subprocess
        assert mock_subprocess.call_count >= 1

    def test_commit_message_format(self, tmp_path: Path) -> None:
        """Commit message follows 'mill(docs): <title> (<ticket-id>)' format."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=True, summary="updated docs"
        )
        _fake_vcs_git_ops.has_changes.return_value = True

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "20250601T120000Z-test-ab12"
        ticket.title = "Fix frobnicator"

        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            stage.run(ticket, ctx)

        commit_call = _fake_vcs_git_ops.commit_all.call_args
        assert commit_call[0][0] == tmp_path / "repo"
        commit_msg = commit_call[0][1]
        assert commit_msg.startswith("mill(docs):")
        assert "Fix frobnicator" in commit_msg
        assert "20250601T120000Z-test-ab12" in commit_msg

    # ------------------------------------------------------------------
    # Full doc agent: user_facing=True, no changes (recommendation-only)
    # ------------------------------------------------------------------

    def test_doc_agent_user_facing_no_edits(self, tmp_path: Path) -> None:
        """When user_facing=True but has_changes=False, stage passes through."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=True,
            summary="recommendation: should update README",
        )
        _fake_vcs_git_ops.has_changes.return_value = False

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Test"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        # Should NOT have called commit_all
        _fake_vcs_git_ops.commit_all.assert_not_called()
        # Should have posted a step event — the classifier posts one
        # ("running full doc agent") and the no-edits path posts another
        # ("recommendation-only doc deliverable").
        assert len(ctx.service.step_events) >= 2
        assert "recommendation-only" in ctx.service.step_events[1][1]

    # ------------------------------------------------------------------
    # Full doc agent: failure → non-blocking pass-through
    # ------------------------------------------------------------------

    def test_doc_agent_failure_passes_through(self, tmp_path: Path) -> None:
        """When doc agent raises, outcome is DELIVERABLE (warn-and-pass)."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.side_effect = RuntimeError(
            "doc agent crashed"
        )

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Test"

        outcome = stage.run(ticket, ctx)
        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert "doc agent failed" in outcome.note

    # ------------------------------------------------------------------
    # Commit failure → non-blocking pass-through
    # ------------------------------------------------------------------

    def test_commit_failure_passes_through(self, tmp_path: Path) -> None:
        """When commit_all raises, outcome is still DELIVERABLE."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=True, summary="updated docs"
        )
        _fake_vcs_git_ops.has_changes.return_value = True
        _fake_vcs_git_ops.commit_all.side_effect = RuntimeError("commit failed")

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Test"

        with patch("subprocess.run") as mock_subprocess:
            mock_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            outcome = stage.run(ticket, ctx)

        assert outcome.state == _FakeStateEnum.DELIVERABLE

    # ------------------------------------------------------------------
    # Classifier: user_facing → add_step_event
    # ------------------------------------------------------------------

    def test_classifier_user_facing_posts_step_event(self, tmp_path: Path) -> None:
        """When classifier says user-facing, a step event is posted."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(
                user_facing=True, classification="user-facing change detected"
            )
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=False, summary="no changes"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "ticket-1"

        stage.run(ticket, ctx)
        assert len(ctx.service.step_events) == 1
        assert ctx.service.step_events[0][0] == "ticket-1"
        assert "running full doc agent" in ctx.service.step_events[0][1]

    # ------------------------------------------------------------------
    # Notification on doc agent failure
    # ------------------------------------------------------------------

    def test_doc_agent_failure_sends_notification(self, tmp_path: Path) -> None:
        """When the doc agent raises, send_notification is called with ERRORED."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.side_effect = RuntimeError(
            "doc agent crashed"
        )

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        stage.run(ticket, ctx)
        _fake_notify.send_notification.assert_called_once()
        call_args = _fake_notify.send_notification.call_args
        assert call_args[0][0] == ticket  # same ticket object
        assert call_args[0][1] == _FakeStateEnum.ERRORED

    # ------------------------------------------------------------------
    # Credential redaction in combined_diff failure
    # ------------------------------------------------------------------

    def test_combined_diff_failure_redacts_credentials(self, tmp_path: Path) -> None:
        """When combined_diff fails, credentials in the error message are redacted."""
        _fake_implemented_repos.combined_diff.side_effect = RuntimeError(
            "token: ghs_secret_token_12345"
        )
        _fake_vcs_git_ops.redact_credentials.side_effect = lambda s: s.replace(
            "ghs_secret_token_12345", "<REDACTED>"
        )

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        outcome = stage.run(ticket, ctx)
        assert "<REDACTED>" in outcome.note
        assert "ghs_secret_token_12345" not in outcome.note

    # ------------------------------------------------------------------
    # Preload paths
    # ------------------------------------------------------------------

    def test_preload_paths_include_readme_and_agent_md(self, tmp_path: Path) -> None:
        """When README.md and AGENT.md exist, they are added to reference_files."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("# Test")
        (repo_dir / "AGENT.md").write_text("# Agent")

        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=False, summary="no changes"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        stage.run(ticket, ctx)
        call_kwargs = _fake_agents_documenting.run_doc_agent.call_args[1]
        ref_files = call_kwargs.get("reference_files")
        assert ref_files is not None
        assert "README.md" in ref_files
        assert "AGENT.md" in ref_files

    def test_preload_paths_skip_missing_docs(self, tmp_path: Path) -> None:
        """When README.md and AGENT.md do not exist, they are not in reference_files."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        # No README.md or AGENT.md created

        # The diff should only contain files that are NOT README.md or
        # AGENT.md, and those files are not on disk either — so the only
        # preload paths come from the diff.
        _fake_vcs_git_ops._paths_from_diff.return_value = {"src/foo.py"}

        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=False, summary="no changes"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"

        stage.run(ticket, ctx)
        call_kwargs = _fake_agents_documenting.run_doc_agent.call_args[1]
        ref_files = call_kwargs.get("reference_files")
        # modified_paths + nothing else (no README/AGENT on disk)
        assert "README.md" not in (ref_files or [])

    # ------------------------------------------------------------------
    # extra_roots for multi-repo tickets
    # ------------------------------------------------------------------

    def test_extra_roots_passed_when_multiple_repos(self, tmp_path: Path) -> None:
        """For multi-repo tickets, extra_roots is a list of secondary repo dirs."""
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()
        repos = [_FakeRepoInfo(repo_a), _FakeRepoInfo(repo_b)]

        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=False, summary="no changes"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path, repos=repos)
        ticket = MagicMock()
        ticket.id = "test-id"

        stage.run(ticket, ctx)
        call_kwargs = _fake_agents_documenting.run_doc_agent.call_args[1]
        extra = call_kwargs.get("extra_roots")
        assert extra is not None
        assert repo_b in extra
        assert repo_a not in extra  # repo_a is the primary

    def test_extra_roots_none_for_single_repo(self, tmp_path: Path) -> None:
        """For a single-repo ticket, extra_roots is None."""
        repo = tmp_path / "repo"
        repo.mkdir()
        repos = [_FakeRepoInfo(repo)]

        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.return_value = _FakeDocResult(
            user_facing=False, summary="no changes"
        )
        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path, repos=repos)
        ticket = MagicMock()
        ticket.id = "test-id"

        stage.run(ticket, ctx)
        call_kwargs = _fake_agents_documenting.run_doc_agent.call_args[1]
        assert call_kwargs.get("extra_roots") is None

    # ------------------------------------------------------------------
    # Stage metadata
    # ------------------------------------------------------------------

    def test_stage_name_is_document(self) -> None:
        """DocumentStage.name is 'document'."""
        assert DocumentStage.name == "document"

    def test_stage_input_state_is_documenting(self) -> None:
        """DocumentStage.input_state is DOCUMENTING."""
        assert DocumentStage.input_state == "DOCUMENTING"

    def test_stage_traced_is_true(self) -> None:
        """DocumentStage.traced is True."""
        assert DocumentStage.traced is True


# ---------------------------------------------------------------------------
# Usage-limit detection + retry
# ---------------------------------------------------------------------------


class TestIsUsageLimitError:
    """Unit tests for ``_is_usage_limit_error``."""

    def test_usage_limit_exceeded_class_name(self) -> None:
        """The pydantic-ai UsageLimitExceeded class name is recognised."""
        assert _document._is_usage_limit_error(
            UsageLimitExceededError("request limit exceeded")
        )

    def test_rate_limit_message(self) -> None:
        """A rate-limit message on a generic exception is recognised."""
        assert _document._is_usage_limit_error(RuntimeError("rate limit exceeded"))

    def test_http_429_message(self) -> None:
        """An HTTP 429 message is recognised."""
        assert _document._is_usage_limit_error(
            RuntimeError("HTTP 429 too many requests")
        )

    def test_claude_sdk_usage_exhausted_name(self) -> None:
        """The Claude-SDK usage-exhausted error name is recognised."""

        class ClaudeSDKUsageExhaustedError(RuntimeError):
            pass

        assert _document._is_usage_limit_error(
            ClaudeSDKUsageExhaustedError("out of usage credits")
        )

    def test_non_usage_limit_is_false(self) -> None:
        """A plain failure is not treated as a usage limit."""
        assert not _document._is_usage_limit_error(RuntimeError("doc agent crashed"))

    def test_wrapped_cause_chain(self) -> None:
        """A usage limit wrapped in an outer exception is recognised."""
        outer = RuntimeError("runner failed")
        outer.__cause__ = UsageLimitExceededError("budget cap")
        assert _document._is_usage_limit_error(outer)


class TestDocAgentUsageLimitRetry:
    """Tests for ``_run_doc_agent_with_retry`` via ``DocumentStage.run``."""

    def test_retries_usage_limit_then_succeeds(self, tmp_path: Path) -> None:
        """A usage-limit failure is retried with backoff, then succeeds."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.side_effect = [
            UsageLimitExceededError("request limit exceeded"),
            _FakeDocResult(user_facing=True, summary="updated docs"),
        ]
        _fake_vcs_git_ops.has_changes.return_value = False

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Test"

        with (
            patch.object(_document.time, "sleep") as mock_sleep,
            patch.object(_document.random, "uniform", return_value=0),
        ):
            outcome = stage.run(ticket, ctx)

        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert _fake_agents_documenting.run_doc_agent.call_count == 2
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == pytest.approx(2.0)

    def test_exhausts_retries_then_passes_through(self, tmp_path: Path) -> None:
        """Persistent usage limits fall back to a non-blocking summary note."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.side_effect = UsageLimitExceededError(
            "request limit exceeded"
        )

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Test"

        with (
            patch.object(_document.time, "sleep") as mock_sleep,
            patch.object(_document.random, "uniform", return_value=0),
        ):
            outcome = stage.run(ticket, ctx)

        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert _fake_agents_documenting.run_doc_agent.call_count == 3
        assert mock_sleep.call_count == 2
        assert "usage limit" in outcome.note
        _fake_notify.send_notification.assert_called_once()

    def test_non_usage_limit_error_not_retried(self, tmp_path: Path) -> None:
        """A non-usage-limit failure re-raises immediately (no retry)."""
        _fake_agents_documenting.run_doc_classifier.return_value = (
            _FakeDocClassifierResult(user_facing=True, classification="user-facing")
        )
        _fake_agents_documenting.run_doc_agent.side_effect = RuntimeError(
            "doc agent crashed"
        )

        stage = DocumentStage()
        ctx = _make_stage_context(tmp_path)
        ticket = MagicMock()
        ticket.id = "test-id"
        ticket.title = "Test"

        with patch.object(_document.time, "sleep") as mock_sleep:
            outcome = stage.run(ticket, ctx)

        assert outcome.state == _FakeStateEnum.DELIVERABLE
        assert _fake_agents_documenting.run_doc_agent.call_count == 1
        mock_sleep.assert_not_called()
        assert "doc agent failed" in outcome.note
