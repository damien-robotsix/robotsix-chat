"""Tests for ``src/robotsix_mill/stages/fix_close_verify.py``.

The module is deliberately stdlib-only so it can be imported directly from
source (mirroring ``tests/stages/test_changelog_gate.py``) without the
``robotsix_mill`` shadow package's sibling imports.
"""

# ruff: noqa: D101, D102

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "robotsix_mill"
    / "stages"
    / "fix_close_verify.py"
)
_spec = importlib.util.spec_from_file_location("fix_close_verify", _SOURCE)
assert _spec is not None, f"Could not load spec for {_SOURCE}"
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

is_fix_ticket = _mod.is_fix_ticket
verify_fix_ticket_landed = _mod.verify_fix_ticket_landed

_TICKET_ID = "20260820T000000Z-test-abcd"
_BRANCH = f"fix/{_TICKET_ID}"


def _ticket(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "id": _TICKET_ID,
        "title": "ci_fix: out-of-scope CI failure — ruff W292",
        "source": "ci_fix_dependency",
        "labels": "ci_fp:deadbeef",
        "branch": _BRANCH,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeForge:
    def __init__(self, merged: bool | None) -> None:
        self._merged = merged
        self.calls: list[str] = []

    def pr_status(self, *, source_branch: str) -> dict[str, object] | None:
        self.calls.append(source_branch)
        if self._merged is None:
            return None
        return {"merged": self._merged}


# ---------------------------------------------------------------------------
# is_fix_ticket
# ---------------------------------------------------------------------------


class TestIsFixTicket:
    def test_none(self) -> None:
        assert is_fix_ticket(None) is False

    @pytest.mark.parametrize(
        "overrides,expected",
        [
            # source kind
            ({"title": "other", "source": "ci_fix_dependency", "labels": ""}, True),
            # title prefix
            ({"title": "ci_fix: ruff W292", "source": "user", "labels": ""}, True),
            # recurring-CI-failure diagnostic title prefix
            (
                {
                    "title": "[diagnostic] recurring CI failure: key=abc (3 tickets)",
                    "source": "agent",
                    "labels": "",
                },
                True,
            ),
            # fingerprint label
            ({"title": "other", "source": "user", "labels": "ci_fp:abc123"}, True),
            # no signal
            ({"title": "other", "source": "user", "labels": ""}, False),
            # empty strings
            ({"title": "", "source": "", "labels": ""}, False),
        ],
    )
    def test_detection(self, overrides: dict[str, object], expected: bool) -> None:
        assert is_fix_ticket(_ticket(**overrides)) is expected


# ---------------------------------------------------------------------------
# verify_fix_ticket_landed
# ---------------------------------------------------------------------------


class TestVerifyFixTicketLanded:
    def _fake_git(self, branch_tip: str | None, target_tip: str | None):
        def _run_git(repo_dir: Path, *args: str) -> str | None:
            joined = " ".join(args)
            if "--verify" in joined and _BRANCH in joined:
                return branch_tip
            if "--verify" in joined:
                return target_tip
            return None

        return _run_git

    def test_non_fix_ticket_passes_through(self, tmp_path: Path) -> None:
        ticket = _ticket(title="not a fix", source="user", labels="")
        assert (
            verify_fix_ticket_landed(
                ticket, tmp_path, branch_prefix="fix/", target_branch="main"
            )
            is None
        )

    def test_missing_repo_dir_escalates(self, tmp_path: Path) -> None:
        assert verify_fix_ticket_landed(
            _ticket(), None, branch_prefix="fix/", target_branch="main"
        )

    def test_nonexistent_repo_dir_escalates(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert verify_fix_ticket_landed(
            _ticket(), missing, branch_prefix="fix/", target_branch="main"
        )

    def test_missing_branch_escalates(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git(None, "abc123"))
        assert verify_fix_ticket_landed(
            _ticket(), tmp_path, branch_prefix="fix/", target_branch="main"
        )

    def test_missing_target_escalates(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git("abc123", None))
        assert verify_fix_ticket_landed(
            _ticket(), tmp_path, branch_prefix="fix/", target_branch="main"
        )

    def test_branch_and_target_differ_passes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git("abc123", "def456"))
        assert (
            verify_fix_ticket_landed(
                _ticket(), tmp_path, branch_prefix="fix/", target_branch="main"
            )
            is None
        )

    def test_identical_tips_without_forge_escalates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git("abc123", "abc123"))
        assert verify_fix_ticket_landed(
            _ticket(), tmp_path, branch_prefix="fix/", target_branch="main"
        )

    def test_identical_tips_merged_pr_passes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git("abc123", "abc123"))
        forge = _FakeForge(merged=True)
        assert (
            verify_fix_ticket_landed(
                _ticket(),
                tmp_path,
                branch_prefix="fix/",
                target_branch="main",
                forge=forge,
            )
            is None
        )
        assert forge.calls == [_BRANCH]

    def test_identical_tips_open_pr_escalates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git("abc123", "abc123"))
        forge = _FakeForge(merged=False)
        assert verify_fix_ticket_landed(
            _ticket(),
            tmp_path,
            branch_prefix="fix/",
            target_branch="main",
            forge=forge,
        )

    def test_identical_tips_no_pr_escalates(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git("abc123", "abc123"))
        forge = _FakeForge(merged=None)
        assert verify_fix_ticket_landed(
            _ticket(),
            tmp_path,
            branch_prefix="fix/",
            target_branch="main",
            forge=forge,
        )

    def test_forge_error_treated_as_no_merge(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(_mod, "_run_git", self._fake_git("abc123", "abc123"))

        class _Boom:
            def pr_status(self, *, source_branch: str) -> dict[str, object] | None:
                raise RuntimeError("forge down")

        assert verify_fix_ticket_landed(
            _ticket(),
            tmp_path,
            branch_prefix="fix/",
            target_branch="main",
            forge=_Boom(),
        )
