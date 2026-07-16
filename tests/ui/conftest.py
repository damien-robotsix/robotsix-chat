"""Shared fixtures for tests/ui/ — UI smoke and integration tests."""

from __future__ import annotations

import pytest

from robotsix_chat.chat.server import _load_ui_html, create_app
from tests.conftest import MockAgent, http_client, mock_app

# Re-export for convenience in test modules.
__all__ = [
    "MockAgent",
    "_load_ui_html",
    "create_app",
    "http_client",
    "mock_app",
]


@pytest.fixture
def ui_html() -> str:
    """Return the rendered index.html with default idle timeout."""
    return _load_ui_html(idle_timeout_minutes=30)


@pytest.fixture
def ui_html_no_idle() -> str:
    """Return the rendered index.html with idle timeout disabled (0)."""
    return _load_ui_html(idle_timeout_minutes=0)


@pytest.fixture
def static_css() -> str:
    """Return the raw chat.css content."""
    from importlib import resources

    return (
        resources.files("robotsix_chat")
        / "ui"
        / "static"
        / "chat.css"
    ).read_text(encoding="utf-8")


@pytest.fixture
def static_js() -> str:
    """Return the raw chat.js content."""
    from importlib import resources

    return (
        resources.files("robotsix_chat")
        / "ui"
        / "static"
        / "chat.js"
    ).read_text(encoding="utf-8")
