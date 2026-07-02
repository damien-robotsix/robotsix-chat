"""Smoke test ensuring the package imports and exposes its version."""

import robotsix_chat


def test_import_package() -> None:
    """The package imports and exposes a version string."""
    assert isinstance(robotsix_chat.__version__, str)
