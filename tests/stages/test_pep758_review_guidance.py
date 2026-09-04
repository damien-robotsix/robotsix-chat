"""Tests for the PEP 758 exception-syntax review guidance patch.

The patch lives in the ``robotsix_mill`` shadow package
(``src/robotsix_mill/__init__.py``) and only activates when the shadow is
imported with the real ``robotsix_mill`` installed (the production
mill-worker setup).  When the mill is absent the whole module skips
cleanly.
"""

from __future__ import annotations

import pytest

_mill = pytest.importorskip("robotsix_mill")

# ``tests/stages/test_document.py`` registers stub ``robotsix_mill.*``
# modules in ``sys.modules`` at import time.  When it is collected first,
# ``importorskip`` returns the stub instead of the real package and these
# tests would exercise fakes.  The real package — and the shadow
# ``__init__`` that hands off to it — always carries a real ``__file__``;
# the stubs do not.
if not getattr(_mill, "__file__", ""):
    pytest.skip(
        "robotsix_mill resolved to sibling-test stubs, not the real package",
        allow_module_level=True,
    )

import robotsix_mill.agents.reviewing as _reviewing  # noqa: E402


def test_pep758_guidance_appended_to_reviewer_system_prompt() -> None:
    """Verifies reviewer prompt guards PEP 758 ``except A, B:`` blockers."""
    prompt = _reviewing.SYSTEM_PROMPT
    assert "PEP 758" in prompt
    assert "except A, B:" in prompt
    assert "requires-python" in prompt
    assert ">= 3.14" in prompt
    assert "ruff format" in prompt
    assert "must not be raised as a hard blocker" in prompt


def test_pep758_guidance_changes_reviewer_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guidance busts cached review verdicts when the prompt changes."""
    from robotsix_mill.stages._stage_cache import reviewer_fingerprint

    monkeypatch.setattr(
        _reviewing,
        "_repo_conventions",
        lambda repo_dir: "fixed-conventions",
    )
    original = _reviewing.SYSTEM_PROMPT
    h_before = reviewer_fingerprint()
    assert len(h_before) == 64  # a real sha256, not an empty failure marker
    monkeypatch.setattr(
        _reviewing, "SYSTEM_PROMPT", original + "\n\nmore reviewer guidance"
    )
    h_after = reviewer_fingerprint()
    assert h_after != h_before
