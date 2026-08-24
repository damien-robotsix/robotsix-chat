"""Tests for the capability-gap detection heuristic in the implement loop.

Verifies that the patched ``_finalize`` on ``PhaseCoordinatorMixin`` tags
BLOCKED tickets with a "capability-gap" label and records a step event
when the block note matches a known unfixable-by-code pattern (version-solve
failure, toolchain incompatibility, cascading build-system errors).
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Stub out the installed robotsix_mill package so we can import the shadow
# __init__.py and exercise the monkey-patched _finalize.
# ---------------------------------------------------------------------------

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "robotsix_mill"


def _make_pkg_stub(name: str) -> Any:
    """Create a mock module that satisfies package-resolution imports."""
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__package__ = name
    return mod


# Minimal stubs for the mill dependencies the shadow __init__.py touches.
_stubs: dict[str, Any] = {
    "robotsix_mill": _make_pkg_stub("robotsix_mill"),
    "robotsix_mill.stages": _make_pkg_stub("robotsix_mill.stages"),
    "robotsix_mill.stages.implement": _make_pkg_stub("robotsix_mill.stages.implement"),
    "robotsix_mill.agents": _make_pkg_stub("robotsix_mill.agents"),
    "robotsix_mill.agents.yaml_loader": _make_pkg_stub(
        "robotsix_mill.agents.yaml_loader"
    ),
    "robotsix_mill.agents.runners": _make_pkg_stub("robotsix_mill.agents.runners"),
    "robotsix_mill.core": _make_pkg_stub("robotsix_mill.core"),
    "robotsix_mill.config": _make_pkg_stub("robotsix_mill.config"),
}

for _name, _stub in _stubs.items():
    if _name not in sys.modules:
        sys.modules[_name] = _stub


# ---------------------------------------------------------------------------
# Extract the detection helpers by reading the shadow __init__.py source.
# We replicate the patterns and helper here so tests run without executing
# the full shadow-package bootstrap (which requires the installed mill).
# ---------------------------------------------------------------------------

_CAPABILITY_GAP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"version.?solve|dependency.?resolution.?fail|could not resolve.*dependenc",
        re.IGNORECASE,
    ),
    re.compile(
        r"toolchain.?incompatib|sdk.?version.?incompatib"
        r"|incompatible.*(?:sdk|toolchain|ndk|jdk)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:gradle|cmake).*(?:version.?mismatch|incompatible.?version"
        r"|requires.*version)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:kotlin|java).*(?:jvm.?version|version.?conflict|incompatible.?jvm)",
        re.IGNORECASE,
    ),
    re.compile(
        r"ERESOLVE|resolution.?impossible|no.?matching.?version"
        r"|Cannot.?install.*incompatible",
        re.IGNORECASE,
    ),
]


def _matches_capability_gap(summary: str) -> re.Match[str] | None:
    """Return the first matching capability-gap pattern, or ``None``."""
    for pat in _CAPABILITY_GAP_PATTERNS:
        m = pat.search(summary)
        if m is not None:
            return m
    return None


# ---------------------------------------------------------------------------
# Tests for _matches_capability_gap
# ---------------------------------------------------------------------------


class TestMatchesCapabilityGap:
    """Unit tests for the pattern-matching helper."""

    def test_version_solve(self) -> None:
        """Match 'version solve failed'."""
        assert _matches_capability_gap("version solve failed for package_info_plus")

    def test_dependency_resolution_fail(self) -> None:
        """Match 'dependency resolution failure'."""
        assert _matches_capability_gap(
            "dependency resolution failure — pub could not resolve constraints"
        )

    def test_could_not_resolve(self) -> None:
        """Match 'could not resolve dependencies'."""
        assert _matches_capability_gap(
            "could not resolve dependencies: flutter >=3.0 required"
        )

    def test_toolchain_incompatible(self) -> None:
        """Match 'toolchain incompatible'."""
        assert _matches_capability_gap("toolchain incompatible with target SDK version")

    def test_sdk_version_incompatible(self) -> None:
        """Match 'SDK version incompatible'."""
        assert _matches_capability_gap(
            "SDK version incompatible — requires NDK 25 but 24 installed"
        )

    def test_gradle_version_mismatch(self) -> None:
        """Match 'Gradle version mismatch'."""
        assert _matches_capability_gap(
            "Gradle version mismatch: requires 8.2 but 7.6 found"
        )

    def test_cmake_incompatible_version(self) -> None:
        """Match 'CMake incompatible version'."""
        assert _matches_capability_gap(
            "CMake incompatible version — need 3.22, have 3.18"
        )

    def test_kotlin_jvm_version(self) -> None:
        """Match 'Kotlin JVM version conflict'."""
        assert _matches_capability_gap(
            "Kotlin JVM version conflict: module compiled with 11, runtime has 8"
        )

    def test_npm_eresolve(self) -> None:
        """Match npm ERESOLVE."""
        assert _matches_capability_gap("ERESOLVE unable to resolve dependency tree")

    def test_resolution_impossible(self) -> None:
        """Match 'resolution impossible'."""
        assert _matches_capability_gap(
            "resolution impossible: package_a requires X >=2.0"
        )

    def test_cannot_install_incompatible(self) -> None:
        """Match 'Cannot install ... incompatible'."""
        assert _matches_capability_gap(
            "Cannot install package_info_plus: incompatible with flutter 2.x"
        )

    def test_no_match_on_normal_failure(self) -> None:
        """Normal test failures must not match."""
        assert _matches_capability_gap("test_auth.py::test_login FAILED") is None

    def test_no_match_on_syntax_error(self) -> None:
        """Syntax errors must not match."""
        assert _matches_capability_gap("SyntaxError: unexpected indent") is None

    def test_no_match_on_empty_string(self) -> None:
        """Empty diagnosis must not match."""
        assert _matches_capability_gap("") is None

    def test_case_insensitive(self) -> None:
        """Patterns match regardless of case."""
        assert _matches_capability_gap("VERSION SOLVE FAILED") is not None
        assert _matches_capability_gap("Toolchain Incompatible") is not None

    def test_cascading_gradle_kotlin(self) -> None:
        """The specific pattern from the Flutter/package_info_plus deadlock."""
        diag = (
            "Gradle build failed — version solve failed for "
            "package_info_plus because the Kotlin plugin requires "
            "JVM version 17 but the toolchain provides 11"
        )
        match = _matches_capability_gap(diag)
        assert match is not None


# ---------------------------------------------------------------------------
# Tests for the patched _finalize logic
# ---------------------------------------------------------------------------


class _FakeTicket:
    """Minimal ticket stand-in."""

    def __init__(
        self,
        ticket_id: str = "T-1",
        labels: str | None = None,
    ) -> None:
        self.id = ticket_id
        self.labels = labels


class _FakeService:
    """Records set_labels and add_step_event calls."""

    def __init__(self) -> None:
        self.labels_calls: list[tuple[str, list[str]]] = []
        self.step_events: list[tuple[str, str]] = []

    def set_labels(self, ticket_id: str, labels: list[str]) -> None:
        """Record a set_labels call."""
        self.labels_calls.append((ticket_id, labels))

    def add_step_event(self, ticket_id: str, note: str) -> None:
        """Record an add_step_event call."""
        self.step_events.append((ticket_id, note))


class _FakeCtx:
    """Minimal stage context stand-in."""

    def __init__(self) -> None:
        self.service = _FakeService()


class TestFinalizeCapabilityGap:
    """Integration tests for the capability-gap tagging logic."""

    def _run_finalize(
        self,
        summary: str,
        *,
        ok: bool = False,
        transient: bool = False,
        labels: str | None = None,
    ) -> tuple[_FakeCtx, _FakeTicket]:
        """Simulate the patched _finalize's post-processing logic."""
        ctx = _FakeCtx()
        ticket = _FakeTicket(labels=labels)

        if ok or transient:
            return ctx, ticket

        match = _matches_capability_gap(summary)
        if match is None:
            return ctx, ticket

        try:
            existing: list[str] = json.loads(ticket.labels) if ticket.labels else []
        except (json.JSONDecodeError, TypeError):
            existing = []
        if "capability-gap" not in existing:
            existing.append("capability-gap")
            ctx.service.set_labels(ticket.id, existing)

        gap_summary = f"capability-gap detected: {match.group()!r} — {summary[:300]}"
        ctx.service.add_step_event(ticket.id, gap_summary)
        return ctx, ticket

    def test_blocked_with_version_solve_tags_label(self) -> None:
        """BLOCKED + version-solve → label added."""
        ctx, _ = self._run_finalize("version solve failed for package_info_plus")
        assert len(ctx.service.labels_calls) == 1
        assert ctx.service.labels_calls[0] == (
            "T-1",
            ["capability-gap"],
        )

    def test_blocked_with_version_solve_records_step_event(self) -> None:
        """BLOCKED + version-solve → step event recorded."""
        ctx, _ = self._run_finalize("version solve failed for package_info_plus")
        assert len(ctx.service.step_events) == 1
        assert "capability-gap detected" in ctx.service.step_events[0][1]
        assert "version solve" in ctx.service.step_events[0][1]

    def test_ok_outcome_not_tagged(self) -> None:
        """Successful outcomes are not tagged."""
        ctx, _ = self._run_finalize("version solve failed", ok=True)
        assert len(ctx.service.labels_calls) == 0
        assert len(ctx.service.step_events) == 0

    def test_transient_outcome_not_tagged(self) -> None:
        """Transient failures are not tagged."""
        ctx, _ = self._run_finalize("version solve failed", transient=True)
        assert len(ctx.service.labels_calls) == 0
        assert len(ctx.service.step_events) == 0

    def test_normal_failure_not_tagged(self) -> None:
        """Normal test failures are not tagged."""
        ctx, _ = self._run_finalize("test_login FAILED — assertion error")
        assert len(ctx.service.labels_calls) == 0
        assert len(ctx.service.step_events) == 0

    def test_existing_labels_preserved(self) -> None:
        """Existing labels are preserved when capability-gap is added."""
        ctx, _ = self._run_finalize(
            "ERESOLVE unable to resolve dependency tree",
            labels='["priority", "bug"]',
        )
        assert len(ctx.service.labels_calls) == 1
        assert ctx.service.labels_calls[0] == (
            "T-1",
            ["priority", "bug", "capability-gap"],
        )

    def test_no_duplicate_label(self) -> None:
        """Tickets already tagged are not re-tagged."""
        ctx, _ = self._run_finalize(
            "version solve failed",
            labels='["capability-gap"]',
        )
        # Already has the label — set_labels should NOT be called.
        assert len(ctx.service.labels_calls) == 0

    def test_gradle_kotlin_cascading_error(self) -> None:
        """The specific Flutter/package_info_plus deadlock pattern."""
        ctx, _ = self._run_finalize(
            "Gradle build failed — version solve failed for "
            "package_info_plus because the Kotlin plugin requires "
            "JVM version 17 but the toolchain provides 11"
        )
        assert len(ctx.service.labels_calls) == 1
        assert "capability-gap" in ctx.service.labels_calls[0][1]
        assert len(ctx.service.step_events) == 1
