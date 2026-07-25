"""Smoke test ensuring the package imports and exposes its version."""

from importlib.metadata import version

import robotsix_chat


def test_import_package() -> None:
    """The package imports and exposes a version string."""
    assert isinstance(robotsix_chat.__version__, str)
    assert robotsix_chat.__version__ == version("robotsix-chat")
