"""Tests for ``src/robotsix_mill/stages/towncrier.py``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

# The ``robotsix_mill`` shadow-package __init__.py requires the real
# ``robotsix_mill`` to be installed.  Since the function under test is
# pure stdlib, import it directly from the source file instead.
_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "robotsix_mill"
    / "stages"
    / "towncrier.py"
)
_spec = importlib.util.spec_from_file_location("towncrier", _SOURCE)
assert _spec is not None, f"Could not load spec for {_SOURCE}"
_towncrier = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_towncrier)
maybe_generate_towncrier_fragment = _towncrier.maybe_generate_towncrier_fragment

# A valid ticket ID and title for reuse across tests.
_TICKET_ID = "20250601T120000Z-test-ticket-ab12"
_TITLE = "Fix a critical bug in the frobnicator"


class TestMaybeGenerateTowncrierFragment:
    """Tests for :func:`maybe_generate_towncrier_fragment`."""

    def test_no_pyproject_toml_returns_false(self, tmp_path: Path) -> None:
        """Returns False when the repo has no pyproject.toml."""
        assert maybe_generate_towncrier_fragment(tmp_path, _TICKET_ID, _TITLE) is False

    def test_no_towncrier_section_returns_false(self, tmp_path: Path) -> None:
        """Returns False when pyproject.toml exists but lacks [tool.towncrier]."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        assert maybe_generate_towncrier_fragment(tmp_path, _TICKET_ID, _TITLE) is False

    def test_malformed_toml_returns_false(self, tmp_path: Path) -> None:
        """Returns False when pyproject.toml contains invalid TOML."""
        (tmp_path / "pyproject.toml").write_text("this is not valid toml {{{")
        assert maybe_generate_towncrier_fragment(tmp_path, _TICKET_ID, _TITLE) is False

    def test_valid_config_creates_fragment(self, tmp_path: Path) -> None:
        """Fragment file is created and its content includes the title."""
        (tmp_path / "pyproject.toml").write_text('[tool.towncrier]\npackage = "test"\n')

        assert maybe_generate_towncrier_fragment(tmp_path, _TICKET_ID, _TITLE) is True

        fragment_file = tmp_path / "changes" / f"{_TICKET_ID}.misc.md"
        assert fragment_file.is_file()
        content = fragment_file.read_text()
        assert _TITLE in content

    def test_custom_directory_in_config(self, tmp_path: Path) -> None:
        """Fragment is written to the custom directory from [tool.towncrier]."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.towncrier]\ndirectory = "news"\n'
        )

        assert maybe_generate_towncrier_fragment(tmp_path, _TICKET_ID, _TITLE) is True

        fragment_file = tmp_path / "news" / f"{_TICKET_ID}.misc.md"
        assert fragment_file.is_file()

    def test_existing_fragment_skips_creation(self, tmp_path: Path) -> None:
        """Returns False when a fragment for the same ticket ID already exists."""
        (tmp_path / "pyproject.toml").write_text('[tool.towncrier]\npackage = "test"\n')
        fragment_dir = tmp_path / "changes"
        fragment_dir.mkdir(parents=True)
        (fragment_dir / f"{_TICKET_ID}.feature.md").write_text("existing\n")

        assert maybe_generate_towncrier_fragment(tmp_path, _TICKET_ID, _TITLE) is False

    def test_oserror_on_write_returns_false(self, tmp_path: Path) -> None:
        """Returns False when writing the fragment raises an OSError."""
        (tmp_path / "pyproject.toml").write_text('[tool.towncrier]\npackage = "test"\n')
        # Create the fragment directory as a file to trigger OSError on mkdir.
        (tmp_path / "changes").write_text("")

        assert maybe_generate_towncrier_fragment(tmp_path, _TICKET_ID, _TITLE) is False

    def test_title_trailing_newline_stripped(self, tmp_path: Path) -> None:
        """Fragment file ends with exactly one newline, even if title has extras."""
        (tmp_path / "pyproject.toml").write_text('[tool.towncrier]\npackage = "test"\n')

        assert (
            maybe_generate_towncrier_fragment(
                tmp_path, _TICKET_ID, "multi\nline\ntitle\n\n"
            )
            is True
        )

        fragment_file = tmp_path / "changes" / f"{_TICKET_ID}.misc.md"
        content = fragment_file.read_text()
        assert content == "multi\nline\ntitle\n"
        assert not content.endswith("\n\n")
