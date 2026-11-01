"""Regression guard for vendoring the chunked @robotsix/ui dist build.

``scripts/check-ui-vendor.sh`` runs in the Dockerfile ``ui`` stage and in
``scripts/vendor-ui.sh``; it must fail when ``vanilla.js`` references a
relative ``./*.js`` chunk that was not vendored alongside it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check-ui-vendor.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _run(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["sh", str(_SCRIPT), str(directory)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


def _write_dist(directory: Path, *, with_chunk: bool) -> None:
    (directory / "vanilla.js").write_text(
        'export { mountConfigPanel, AppShell } from "./appshell-CNhMeJsn.js";\n'
    )
    (directory / "style.css").write_text("body {}\n")
    if with_chunk:
        (directory / "appshell-CNhMeJsn.js").write_text(
            "import{a as b}from'./util-Ab12.js';export const mountConfigPanel=b;\n"
        )
        (directory / "util-Ab12.js").write_text("export const a = 1;\n")


def test_passes_when_all_chunks_present(tmp_path: Path) -> None:
    _write_dist(tmp_path, with_chunk=True)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_fails_when_referenced_chunk_missing(tmp_path: Path) -> None:
    _write_dist(tmp_path, with_chunk=False)
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "appshell-CNhMeJsn.js" in result.stderr


def test_fails_when_transitive_chunk_missing(tmp_path: Path) -> None:
    _write_dist(tmp_path, with_chunk=True)
    (tmp_path / "util-Ab12.js").unlink()
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "util-Ab12.js" in result.stderr


def test_fails_when_entry_files_missing(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "vanilla.js" in result.stderr
    assert "style.css" in result.stderr


def test_build_paths_vendor_whole_dist() -> None:
    """Dockerfile and vendor-ui.sh copy the whole dist/, never single files."""
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text()
    assert "@robotsix/ui/dist/ \\" in dockerfile
    assert "dist/vanilla.js \\" not in dockerfile
    assert "check-ui-vendor.sh" in dockerfile
    vendor_sh = (_REPO_ROOT / "scripts" / "vendor-ui.sh").read_text()
    assert "dist/. " in vendor_sh
    assert "check-ui-vendor.sh" in vendor_sh
