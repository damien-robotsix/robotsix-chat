"""Tests for ``src/robotsix_mill/stages/refine_auto_merge_preflight.py``.

The ``robotsix_mill`` shadow-package ``__init__.py`` requires the real
``robotsix_mill`` to be installed.  Since the functions under test are pure
stdlib (plus best-effort duck-typed ``ctx``/``ticket``/``settings``
objects), import them directly from the source file instead — mirroring
``tests/stages/test_changelog_gate.py`` and ``tests/stages/test_towncrier.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "robotsix_mill"
    / "stages"
    / "refine_auto_merge_preflight.py"
)
_spec = importlib.util.spec_from_file_location("refine_auto_merge_preflight", _SOURCE)
assert _spec is not None, f"Could not load spec for {_SOURCE}"
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

_extract_file_paths_from_draft = _mod._extract_file_paths_from_draft
_match_sensitive_globs = _mod._match_sensitive_globs
run_auto_merge_preflight = _mod.run_auto_merge_preflight


# ---------------------------------------------------------------------------
# _extract_file_paths_from_draft
# ---------------------------------------------------------------------------


class TestExtractFilePathsFromDraft:
    """Path extraction from backtick-quoted spans in draft text."""

    def test_extracts_slash_paths_and_known_extensions(self) -> None:
        """Paths with a slash or a known extension are extracted in order."""
        draft = "Edit `src/app.py` and `config/x.yaml`, mention `README`."
        assert _extract_file_paths_from_draft(draft) == [
            "src/app.py",
            "config/x.yaml",
        ]

    def test_bare_extension_without_slash_is_matched(self) -> None:
        """A slash-less token with a known extension still matches."""
        assert _extract_file_paths_from_draft("touch `setup.cfg`") == ["setup.cfg"]

    def test_non_path_backtick_spans_are_ignored(self) -> None:
        """Backtick spans that are not path-like are ignored."""
        draft = "The function `do_thing` returns `True`."
        assert _extract_file_paths_from_draft(draft) == []

    def test_unquoted_paths_are_ignored(self) -> None:
        """Only backtick-quoted spans are considered."""
        assert _extract_file_paths_from_draft("edit src/app.py directly") == []

    def test_multiline_draft(self) -> None:
        """Paths are found across multiple lines."""
        draft = "Line one `a/b.py`\nLine two `c/d.md`\n"
        assert _extract_file_paths_from_draft(draft) == ["a/b.py", "c/d.md"]

    def test_empty_draft_returns_empty(self) -> None:
        """An empty draft yields no paths."""
        assert _extract_file_paths_from_draft("") == []


# ---------------------------------------------------------------------------
# _match_sensitive_globs
# ---------------------------------------------------------------------------


class TestMatchSensitiveGlobs:
    """Glob matching of extracted paths against sensitive patterns."""

    def test_matches_returns_path_and_glob_pairs(self) -> None:
        """Each matching path is reported with the glob it matched."""
        paths = ["src/a.py", ".github/workflows/ci.yml"]
        globs = ["src/*.py", ".github/*/*.yml"]
        assert _match_sensitive_globs(paths, globs) == [
            ("src/a.py", "src/*.py"),
            (".github/workflows/ci.yml", ".github/*/*.yml"),
        ]

    def test_non_matching_paths_excluded(self) -> None:
        """Paths that match no glob are excluded."""
        assert _match_sensitive_globs(["docs/readme.md"], ["*.py"]) == []

    def test_first_matching_glob_wins(self) -> None:
        """A path matching multiple globs is reported once, against the first."""
        matched = _match_sensitive_globs(["a.py"], ["*.py", "a.*"])
        assert matched == [("a.py", "*.py")]

    def test_empty_inputs(self) -> None:
        """Empty path or glob lists yield no matches."""
        assert _match_sensitive_globs([], ["*.py"]) == []
        assert _match_sensitive_globs(["a.py"], []) == []


# ---------------------------------------------------------------------------
# run_auto_merge_preflight
# ---------------------------------------------------------------------------


class _FakeService:
    """Records comments posted via ``add_comment``."""

    def __init__(self) -> None:
        """Initialise with an empty comment log."""
        self.comments: list[tuple[str, str]] = []

    def add_comment(self, ticket_id: str, body: str) -> None:
        """Record a posted comment."""
        self.comments.append((ticket_id, body))


def _ctx(service: _FakeService, repo_id: str | None) -> Any:
    """Build a duck-typed stage context with the given service/repo."""
    repo_config = None if repo_id is None else SimpleNamespace(repo_id=repo_id)
    return SimpleNamespace(service=service, repo_config=repo_config)


def _settings(**kw: Any) -> Any:
    """Build a duck-typed settings object with auto-merge defaults."""
    base: dict[str, Any] = {
        "auto_merge_enabled": True,
        "auto_merge_kill_switch": False,
        "auto_merge_infra_denylist": [],
        "auto_merge_sensitive_globs": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestRunAutoMergePreflight:
    """End-to-end advisory-posting behaviour of the preflight runner."""

    def test_no_issues_posts_no_comment(self) -> None:
        """When nothing blocks auto-merge, no advisory is posted."""
        svc = _FakeService()
        run_auto_merge_preflight(
            _ctx(svc, "ok/repo"),
            SimpleNamespace(id="T1"),
            "touches `README.md`",
            _settings(auto_merge_sensitive_globs=["*.py"]),
        )
        assert svc.comments == []

    def test_globally_disabled_posts_advisory(self) -> None:
        """A globally-disabled auto-merge triggers an advisory."""
        svc = _FakeService()
        run_auto_merge_preflight(
            _ctx(svc, "ok/repo"),
            SimpleNamespace(id="T1"),
            "",
            _settings(auto_merge_enabled=False),
        )
        assert len(svc.comments) == 1
        assert "globally disabled" in svc.comments[0][1]

    def test_kill_switch_posts_advisory(self) -> None:
        """An engaged kill-switch triggers an advisory."""
        svc = _FakeService()
        run_auto_merge_preflight(
            _ctx(svc, "ok/repo"),
            SimpleNamespace(id="T1"),
            "",
            _settings(auto_merge_kill_switch=True),
        )
        assert "kill-switch" in svc.comments[0][1]

    def test_repo_denylist_posts_advisory(self) -> None:
        """A denylisted repo triggers an advisory."""
        svc = _FakeService()
        run_auto_merge_preflight(
            _ctx(svc, "robotsix/infra"),
            SimpleNamespace(id="T1"),
            "",
            _settings(auto_merge_infra_denylist=["robotsix/infra"]),
        )
        assert "denylist" in svc.comments[0][1]

    def test_sensitive_glob_match_posts_advisory(self) -> None:
        """A draft path matching a sensitive glob triggers an advisory."""
        svc = _FakeService()
        run_auto_merge_preflight(
            _ctx(svc, "ok/repo"),
            SimpleNamespace(id="T1"),
            "edit `src/secret.py`",
            _settings(auto_merge_sensitive_globs=["src/*.py"]),
        )
        body = svc.comments[0][1]
        assert "auto-merge-sensitive" in body
        assert "src/secret.py" in body

    def test_missing_repo_config_skips_denylist_gate(self) -> None:
        """A missing repo_config skips the denylist gate without error."""
        svc = _FakeService()
        run_auto_merge_preflight(
            _ctx(svc, None),
            SimpleNamespace(id="T1"),
            "",
            _settings(auto_merge_infra_denylist=["anything"]),
        )
        assert svc.comments == []

    def test_comment_failure_is_swallowed(self) -> None:
        """A failing ``add_comment`` is logged, not raised."""

        class _BoomService:
            def add_comment(self, ticket_id: str, body: str) -> None:
                raise RuntimeError("network down")

        # Should not raise even though add_comment blows up.
        run_auto_merge_preflight(
            _ctx(_BoomService(), "ok/repo"),  # type: ignore[arg-type]
            SimpleNamespace(id="T1"),
            "",
            _settings(auto_merge_enabled=False),
        )
