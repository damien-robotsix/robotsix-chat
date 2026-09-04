"""Tests for the render_url tool — :func:`build_render_url_tools`.

Mock Playwright via ``sys.modules`` injection so tests never need a real
browser or the ``playwright`` package installed.
"""

from __future__ import annotations

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


def _tools(**kw: Any) -> list[Any]:
    """Build the render_url tool."""
    # Imported lazily, like the tests do — the fake Playwright must be
    # installed before the module is first imported.
    from robotsix_chat.render_url import build_render_url_tools

    return build_render_url_tools(_settings(**kw))


def _fake_playwright_module() -> Any:
    """Return a mock ``playwright.async_api`` module with a fake browser chain."""
    a11y_tree = '- document\n  - heading "Hello" [level=1]\n  - link "Click me"\n'
    png_bytes = b"\x89PNG\r\n\x1a\nfake"

    # page.locator("body") returns a locator with .aria_snapshot()
    mock_locator = MagicMock()
    mock_locator.aria_snapshot = AsyncMock(return_value=a11y_tree)

    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.title = AsyncMock(return_value="Test Page Title")
    mock_page.url = "https://example.com/page"
    mock_page.screenshot = AsyncMock(return_value=png_bytes)
    mock_page.locator = MagicMock(return_value=mock_locator)

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
    assert s.enabled is True
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


def _split_result(result):
    """Split render_url's return into (metadata dict, image bytes | None)."""
    if isinstance(result, str):
        return json.loads(result), None
    text_part, image_part = result
    assert image_part.media_type == "image/png"
    return json.loads(text_part.content), image_part.data


# ---------------------------------------------------------------------------
# render_url — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_success() -> None:
    """render_url returns a viewable image part plus JSON metadata.

    Regression: the screenshot used to be base64'd into the JSON string, so
    the transport stringified it and the model got an unreadable text blob
    instead of an image.
    """
    fake = _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        result = await render_url("https://example.com/page")
        metadata, image = _split_result(result)

        assert metadata["page_title"] == "Test Page Title"
        assert metadata["page_url"] == "https://example.com/page"
        assert metadata["error"] == ""
        assert metadata["accessibility_tree"] == fake._test_a11y
        # The PNG rides as raw bytes, not text.
        assert image == fake._test_png
        # No base64 payload anywhere in the text channel.
        text = json.dumps(metadata)
        assert "base64" not in text
        assert "screenshot_base64" not in metadata

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

        result = await render_url("https://bad.example.com")
        metadata, image = _split_result(result)

        assert metadata["error"] == "Exception: net::ERR_CONNECTION_REFUSED"
        assert metadata["page_title"] == ""
        # A failed render has no image to show — plain metadata, no empty part.
        assert image is None
        assert isinstance(result, str)

        fake._test_browser.close.assert_awaited_once()
    finally:
        _remove_fake_playwright()


@pytest.mark.asyncio
async def test_render_url_missing_accessibility_tree() -> None:
    """When ARIA snapshot returns empty, a11y_tree stays null."""
    fake = _install_fake_playwright()
    # Simulate an empty ARIA snapshot: mock the locator's aria_snapshot.
    mock_body_locator = MagicMock()
    mock_body_locator.aria_snapshot = AsyncMock(return_value="")
    fake._test_page.locator = MagicMock(return_value=mock_body_locator)
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        result = await render_url("https://example.com")
        metadata, _image = _split_result(result)

        assert metadata["error"] == ""
        assert metadata["accessibility_tree"] is None
    finally:
        _remove_fake_playwright()


# ---------------------------------------------------------------------------
# render_url — text_only mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_text_only_skips_screenshot() -> None:
    """text_only=True omits the screenshot but still returns a11y tree."""
    fake = _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        result = await render_url("https://example.com/page", text_only=True)
        metadata, image = _split_result(result)

        assert metadata["page_title"] == "Test Page Title"
        assert metadata["page_url"] == "https://example.com/page"
        assert metadata["error"] == ""
        # No image part at all in text-only mode — a bare JSON string.
        assert image is None
        assert isinstance(result, str)
        # Accessibility tree must still be present.
        assert metadata["accessibility_tree"] == fake._test_a11y

        # page.screenshot must NOT have been called.
        fake._test_page.screenshot.assert_not_called()
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


# ---------------------------------------------------------------------------
# render_url — fleet auth
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# render_url — screenshot size bounding
# ---------------------------------------------------------------------------


def _png_bytes(width: int, height: int) -> bytes:
    """Return a real PNG of the given size."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_downscale_png_leaves_a_small_image_untouched() -> None:
    from robotsix_chat.render_url import _downscale_png

    data = _png_bytes(400, 300)

    assert _downscale_png(data) is data


def test_downscale_png_shrinks_an_oversized_capture() -> None:
    """A full_page capture of a long document is otherwise unbounded."""
    import io

    from PIL import Image

    from robotsix_chat.render_url import MAX_SCREENSHOT_PIXELS, _downscale_png

    data = _png_bytes(1280, 12000)  # 15.4M px — a long scrolling page
    assert MAX_SCREENSHOT_PIXELS < 1280 * 12000

    result = _downscale_png(data)

    with Image.open(io.BytesIO(result)) as image:
        width, height = image.size
    assert width * height <= MAX_SCREENSHOT_PIXELS
    # Aspect ratio preserved, so layout still reads correctly.
    assert abs((width / height) - (1280 / 12000)) < 0.01


def test_downscale_png_returns_input_on_undecodable_data() -> None:
    """An oversized screenshot beats a hard failure."""
    from robotsix_chat.render_url import _downscale_png

    garbage = b"not a png"

    assert _downscale_png(garbage) == garbage


@pytest.mark.asyncio
async def test_render_url_bounds_the_returned_screenshot() -> None:
    """An enormous page screenshot is downscaled before it reaches the model."""
    import io

    from PIL import Image

    fake = _install_fake_playwright()
    fake._test_page.screenshot = AsyncMock(return_value=_png_bytes(1280, 12000))
    try:
        from robotsix_chat.render_url import (
            MAX_SCREENSHOT_PIXELS,
            build_render_url_tools,
        )

        tools = build_render_url_tools(_settings())
        render_url = tools[0]

        result = await render_url("https://example.com/long")
        _metadata, image = _split_result(result)

        assert image is not None
        with Image.open(io.BytesIO(image)) as rendered:
            width, height = rendered.size
        assert width * height <= MAX_SCREENSHOT_PIXELS
    finally:
        _remove_fake_playwright()


# ---------------------------------------------------------------------------
# render_url — caption-or-omit path (text-only serving model)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_url_captions_screenshot_when_vision_configured() -> None:
    """A configured vision model yields a caption in place of IMAGE_OMITTED_NOTE."""
    from unittest.mock import AsyncMock, patch

    from robotsix_chat.llm.capabilities import (
        reset_model_supports_images,
        set_model_supports_images,
    )

    _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(
            _settings(),
            vision_model="openrouter/openai/gpt-4o-mini",
            vision_api_key="key-123",
        )
        render_url = tools[0]

        token = set_model_supports_images(False)
        try:
            with patch(
                "robotsix_chat.render_url.caption_or_omit_note",
                new=AsyncMock(
                    return_value='{"page_title": "T"}\n[Image caption: a dashboard]'
                ),
            ) as mock_caption:
                result = await render_url("https://example.com")
        finally:
            reset_model_supports_images(token)

        assert "[Image caption: a dashboard]" in result
        assert "Image omitted" not in result
        assert mock_caption.await_count == 1
        call_kwargs = mock_caption.await_args.kwargs
        assert call_kwargs["vision_model"] == "openrouter/openai/gpt-4o-mini"
        assert call_kwargs["vision_api_key"] == "key-123"
    finally:
        _remove_fake_playwright()


@pytest.mark.asyncio
async def test_render_url_omit_note_when_no_vision_model() -> None:
    """Without a vision model the curated omit-note path is unchanged."""
    from robotsix_chat.llm.capabilities import (
        IMAGE_OMITTED_NOTE,
        reset_model_supports_images,
        set_model_supports_images,
    )

    _install_fake_playwright()
    try:
        from robotsix_chat.render_url import build_render_url_tools

        tools = build_render_url_tools(_settings())  # no vision_model
        render_url = tools[0]

        token = set_model_supports_images(False)
        try:
            result = await render_url("https://example.com")
        finally:
            reset_model_supports_images(token)

        assert isinstance(result, str)
        assert IMAGE_OMITTED_NOTE in result
        assert "Image caption:" not in result
    finally:
        _remove_fake_playwright()
