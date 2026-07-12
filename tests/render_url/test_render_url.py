"""Tests for the render_url tool — :func:`build_render_url_tools`.

Mock Playwright via ``sys.modules`` injection so tests never need a real
browser or the ``playwright`` package installed.
"""

from __future__ import annotations

import base64
import importlib
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.config.models import RenderUrlSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**kw: Any) -> RenderUrlSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return RenderUrlSettings(**base)


def _fake_playwright_module() -> Any:
    """Return a mock ``playwright.async_api`` module with a fake browser chain."""
    a11y_tree: dict[str, Any] = {
        "role": "WebArea",
        "name": "Test Page",
        "children": [
            {"role": "heading", "name": "Hello", "level": 1},
            {"role": "link", "name": "Click me"},
        ],
    }
    png_bytes = b"\x89PNG\r\n\x1a\nfake"

    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.title = AsyncMock(return_value="Test Page Title")
    mock_page.url = "https://example.com/page"
    mock_page.screenshot = AsyncMock(return_value=png_bytes)
    mock_page.accessibility.snapshot = AsyncMock(return_value=a11y_tree)

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()

    mock_browser = MagicMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_chromium = MagicMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw = MagicMock()
    mock_pw.chromium = mock_chromium

    # async_playwright() returns an async context manager.
    mock_async_pw = MagicMock()
    mock_async_pw.__aenter__ = AsyncMock(return_value=mock_pw)
    mock_async_pw.__aexit__ = AsyncMock(return_value=None)

    module = MagicMock()
    module.async_playwright = MagicMock(return_value=mock_async_pw)

    # Stash references the tests can inspect.
    module._test_page = mock_page
    module._test_browser = mock_browser
    module._test_pw = mock_pw
    module._test_png = png_bytes
    module._test_a11y = a11y_tree

    return module


def _install_fake_playwright() -> MagicMock:
    """Inject a fake ``playwright.async_api`` into ``sys.modules`` and reload."""
    fake = _fake_playwright_module()
    # Ensure the parent chain exists for the import machinery.
    if "playwright" not in sys.modules:
        parent = MagicMock()
        sys.modules["playwright"] = parent
    sys.modules["playwright.async_api"] = fake
    importlib.reload(sys.modules["robotsix_chat.render_url"])
    return fake


def _remove_fake_playwright() -> None:
    """Restore sys.modules and reload the real render_url module."""
    sys.modules.pop("playwright.async_api", None)
    importlib.reload(sys.modules["robotsix_chat.render_url"])


# ---------------------------------------------------------------------------
# RenderUrlSettings
# ---------------------------------------------------------------------------


def test_render_url_settings_defaults() -> None:
    """Default RenderUrlSettings has sensible values."""
    s = RenderUrlSettings()
    assert s.enabled is False
    assert s.timeout == 30.0
    assert s.viewport_width == 1280
    assert s.viewport_height == 720


# ---------------------------------------------------------------------------
# build_render_url_tools — disabled / import failure
# ---------------------------------------------------------------------------


def test_build_render_url_tools_disabled() -> None:
    """Disabled render_url returns no tools."""
    from robotsix_chat.render_url import build_render_url_tools

    assert build_render_url_tools(RenderUrlSettings(enabled=False)) == []


def test_build_render_url_tools_playwright_not_installed() -> None:
    """When playwright is not importable, returns [] even if enabled."""
    # Remove any lingering fake.
    sys.modules.pop("playwright.async_api", None)
    sys.modules.pop("playwright", None)
    importlib.reload(sys.modules["robotsix_chat.render_url"])

    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        assert tools == []
    finally:
        # Clean up so later tests can install the fake.
        pass


# ---------------------------------------------------------------------------
# build_render_url_tools — enabled
# ---------------------------------------------------------------------------


def test_build_render_url_tools_returns_one_tool() -> None:
    """Enabled render_url returns a single tool named render_url."""
    _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        assert len(tools) == 1
        assert tools[0].__name__ == "render_url"
    finally:
        _remove_fake_playwright()


# ---------------------------------------------------------------------------
# render_url — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_success() -> None:
    """render_url returns JSON with screenshot and a11y tree on success."""
    fake = _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        result_str = await render_url("https://example.com/page")
        result = json.loads(result_str)

        assert result["page_title"] == "Test Page Title"
        assert result["page_url"] == "https://example.com/page"
        assert result["error"] == ""
        assert result["screenshot_base64"].startswith("data:image/png;base64,")
        decoded = base64.b64decode(
            result["screenshot_base64"].removeprefix("data:image/png;base64,")
        )
        assert decoded == fake._test_png
        assert result["accessibility_tree"] == fake._test_a11y

        fake._test_page.goto.assert_awaited_once_with(
            "https://example.com/page",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
    finally:
        _remove_fake_playwright()


# ---------------------------------------------------------------------------
# render_url — custom viewport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_custom_viewport() -> None:
    """Custom viewport dimensions are passed to the browser context."""
    fake = _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(
            _settings(viewport_width=1920, viewport_height=1080)
        )
        render_url = tools[0]

        await render_url("https://example.com")

        fake._test_browser.new_context.assert_awaited_once_with(
            viewport={"width": 1920, "height": 1080}
        )
    finally:
        _remove_fake_playwright()


# ---------------------------------------------------------------------------
# render_url — custom timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_custom_timeout() -> None:
    """Custom timeout is converted to ms and passed to page.goto."""
    fake = _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings(timeout=15.0))
        render_url = tools[0]

        await render_url("https://example.com")

        fake._test_page.goto.assert_awaited_once_with(
            "https://example.com",
            wait_until="domcontentloaded",
            timeout=15_000,
        )
    finally:
        _remove_fake_playwright()


# ---------------------------------------------------------------------------
# render_url — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_navigation_error() -> None:
    """A navigation error is caught and returned in the JSON error field."""
    fake = _install_fake_playwright()
    fake._test_page.goto = AsyncMock(
        side_effect=Exception("net::ERR_CONNECTION_REFUSED")
    )
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        result_str = await render_url("https://bad.example.com")
        result = json.loads(result_str)

        assert result["error"] == "Exception: net::ERR_CONNECTION_REFUSED"
        assert result["screenshot_base64"] == ""
        assert result["page_title"] == ""

        fake._test_browser.close.assert_awaited_once()
    finally:
        _remove_fake_playwright()


@pytest.mark.asyncio
async def test_render_url_missing_accessibility_tree() -> None:
    """When accessibility snapshot returns None, a11y_tree stays null."""
    fake = _install_fake_playwright()
    fake._test_page.accessibility.snapshot = AsyncMock(return_value=None)
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        result_str = await render_url("https://example.com")
        result = json.loads(result_str)

        assert result["error"] == ""
        assert result["accessibility_tree"] is None
    finally:
        _remove_fake_playwright()


# ---------------------------------------------------------------------------
# render_url — browser launch args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_launches_headless_with_no_sandbox() -> None:
    """Chromium is launched headless with --no-sandbox args (container-friendly)."""
    fake = _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        await render_url("https://example.com")

        fake._test_pw.chromium.launch.assert_awaited_once_with(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
    finally:
        _remove_fake_playwright()
