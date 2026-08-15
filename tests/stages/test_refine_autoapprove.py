"""Tests for ``src/robotsix_mill/stages/refine_autoapprove.py``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# The ``robotsix_mill`` shadow-package __init__.py requires the real
# ``robotsix_mill`` to be installed.  Since the module under test is a pure
# data constant, import it directly from the source file instead (mirroring
# tests/stages/test_towncrier.py).
_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "robotsix_mill"
    / "stages"
    / "refine_autoapprove.py"
)
_spec = importlib.util.spec_from_file_location("refine_autoapprove", _SOURCE)
assert _spec is not None, f"Could not load spec for {_SOURCE}"
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)


def test_chat_agent_source_is_in_extension_set() -> None:
    """The robotsix-chat assistant's ingest source skips the approval gate."""
    assert "robotsix-chat" in _mod.EXTRA_AUTO_APPROVE_SOURCES


def test_extension_is_immutable() -> None:
    """The extension is declared as an immutable frozenset."""
    assert isinstance(_mod.EXTRA_AUTO_APPROVE_SOURCES, frozenset)
