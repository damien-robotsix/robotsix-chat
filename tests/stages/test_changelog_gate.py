"""Tests for ``src/robotsix_mill/stages/changelog_gate.py``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# The ``robotsix_mill`` shadow-package __init__.py requires the real
# ``robotsix_mill`` to be installed.  Since the functions under test are
# pure stdlib, import them directly from the source file instead (mirroring
# tests/stages/test_towncrier.py).
_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "robotsix_mill"
    / "stages"
    / "changelog_gate.py"
)
_spec = importlib.util.spec_from_file_location("changelog_gate", _SOURCE)
assert _spec is not None, f"Could not load spec for {_SOURCE}"
_gate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_gate)
run_changelog_fragment_gate = _gate.run_changelog_fragment_gate

_TICKET_ID = "20260815T015647Z-test-gate-ab12"


class _FakeProc:
    """Minimal stand-in for a completed ``subprocess.run`` result."""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.returncode = 0


def _write_fake_verify_script(
    repo: Path, *, exit_code: int = 0, stderr: str = ""
) -> None:
    """Write a fake verification script that records its argv and exits."""
    scripts = repo / "scripts"
    scripts.mkdir()
    payload = (
        "import sys\n"
        "from pathlib import Path\n"
        "Path(__file__).resolve().parent.parent.joinpath('argv.txt').write_text"
        "(' '.join(sys.argv[1:]))\n"
        f"sys.stderr.write({stderr!r})\n"
        "sys.exit(0 if '--skip' in sys.argv else " + str(exit_code) + ")\n"
    )
    (scripts / "verify_changelog_fragment.py").write_text(payload)


def test_script_absent_passes_through(tmp_path: Path) -> None:
    """A repo without the verification script is not gated."""
    assert run_changelog_fragment_gate(tmp_path, _TICKET_ID) is None


def test_internal_only_change_short_circuits(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Internal-only changes run the script with ``--skip`` and pass."""
    monkeypatch.setattr(_gate, "_is_internal_only_change", lambda _: True)
    _write_fake_verify_script(tmp_path, exit_code=1)

    assert run_changelog_fragment_gate(tmp_path, _TICKET_ID) is None
    assert "--skip" in (tmp_path / "argv.txt").read_text()


def test_user_facing_missing_fragment_returns_error(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A user-facing change with no fragment returns a blocker error."""
    monkeypatch.setattr(_gate, "_is_internal_only_change", lambda _: False)
    _write_fake_verify_script(
        tmp_path,
        exit_code=1,
        stderr=f"ERROR: no changelog fragment found for {_TICKET_ID!r}\n",
    )

    error = run_changelog_fragment_gate(tmp_path, _TICKET_ID)

    assert error is not None
    assert "changelog fragment verification failed" in error
    assert _TICKET_ID in error
    assert "add_changelog_fragment" in error
    assert "--skip" not in (tmp_path / "argv.txt").read_text()


def test_user_facing_fragment_present_passes(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A user-facing change whose fragment exists passes the gate."""
    monkeypatch.setattr(_gate, "_is_internal_only_change", lambda _: False)
    _write_fake_verify_script(tmp_path, exit_code=0)

    assert run_changelog_fragment_gate(tmp_path, _TICKET_ID) is None


def test_is_internal_path_boundaries() -> None:
    """The internal-only classification covers tooling but not src/docs."""
    internal = [
        "tests/test_gate.py",
        ".github/workflows/ci.yml",
        "scripts/verify_changelog_fragment.py",
        "src/robotsix_mill/stages/changelog_gate.py",
        "docs/modules.yaml",
        "pyproject.toml",
    ]
    user_facing = [
        "src/robotsix_chat/chat/server/app.py",
        "docs/configuration.md",
        "config/config.json",
        "deploy/docker-compose.yml",
    ]
    for path in internal:
        assert _gate._is_internal_path(path), path
    for path in user_facing:
        assert not _gate._is_internal_path(path), path


def test_is_internal_only_change(tmp_path: Path, monkeypatch: object) -> None:
    """Empty and all-internal diffs skip; any user-facing path does not."""
    monkeypatch.setattr(_gate, "_changed_paths", lambda _: set())
    assert _gate._is_internal_only_change(tmp_path) is True

    monkeypatch.setattr(_gate, "_changed_paths", lambda _: {"tests/test_gate.py"})
    assert _gate._is_internal_only_change(tmp_path) is True

    monkeypatch.setattr(
        _gate,
        "_changed_paths",
        lambda _: {"tests/test_gate.py", "src/robotsix_chat/chat/server/app.py"},
    )
    assert _gate._is_internal_only_change(tmp_path) is False


def test_changed_paths_merges_tracked_and_untracked(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Tracked modifications and untracked files are unioned."""

    def fake_run(argv, **kwargs: object) -> _FakeProc:
        if argv == ["git", "diff", "--name-only", "HEAD"]:
            return _FakeProc("src/robotsix_chat/chat/server/app.py\n")
        if argv == ["git", "ls-files", "--others", "--exclude-standard"]:
            return _FakeProc("changelog.d/entry.misc.md\n")
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(_gate.subprocess, "run", fake_run)

    assert _gate._changed_paths(tmp_path) == {
        "src/robotsix_chat/chat/server/app.py",
        "changelog.d/entry.misc.md",
    }
