"""Read-only URL rendering via headless Chromium (Playwright).

Returns a *viewable* screenshot plus the ARIA accessibility tree so the
agent can visually inspect a rendered page.  No state mutation or form
submission — strictly read-only: the page is loaded, a full-page screenshot
is captured, the ARIA snapshot (a11y tree) is extracted, and the browser is
closed immediately.

The screenshot is returned as a pydantic-ai ``BinaryContent`` part, which
robotsix-llmio maps onto a native MCP ``image`` block.  It must NOT be
base64-encoded into a JSON string: the transport stringifies whatever it is
given, so a data URL inside JSON reaches the model as an unreadable text
blob (a 205 KB PNG became 588,111 characters) rather than an image.

Requires the ``render-url`` extra (``playwright``) and a Playwright
Chromium browser installation.  When Playwright is not importable the
factory returns an empty list (graceful degradation).
"""

from __future__ import annotations

__all__ = ["build_render_url_tools", "load_render_url_skill"]

import io
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from robotsix_chat.config.models import RenderUrlSettings

logger = logging.getLogger(__name__)

#: Pixel budget for the returned screenshot (~750k px ≈ a few hundred KB of
#: PNG).  A ``full_page`` capture of a long document is otherwise unbounded —
#: an infinite-scroll page can produce a multi-megabyte image that blows the
#: model's per-image limit.  Downscaling preserves layout, which is what the
#: screenshot is for; fine print stays available via the a11y tree.
MAX_SCREENSHOT_PIXELS = 750_000


def load_render_url_skill() -> str:
    """Return the render_url component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _downscale_png(data: bytes, max_pixels: int = MAX_SCREENSHOT_PIXELS) -> bytes:
    """Return *data* shrunk to fit *max_pixels*, or unchanged when it fits.

    Returns the input untouched when Pillow is unavailable or the image
    cannot be decoded — an oversized screenshot is still far more useful
    than a hard failure.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the render extra
        return data

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            pixels = width * height
            if pixels <= max_pixels:
                return data
            ratio = (max_pixels / pixels) ** 0.5
            resized = image.resize(
                (max(1, int(width * ratio)), max(1, int(height * ratio))),
                resample=Image.Resampling.LANCZOS,
            )
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG")
    except Exception:
        logger.warning("render_url: could not downscale screenshot", exc_info=True)
        return data
    return buffer.getvalue()


def build_render_url_tools(
    settings: RenderUrlSettings,
) -> list[Callable[..., Any]]:
    """Return the ``render_url`` tool, or an empty list when disabled.

    Args:
        settings: RenderUrl configuration (``enabled`` master switch,
            timeout, viewport dimensions).

    Returns:
        A single-element list containing the ``render_url`` async callable,
        or ``[]`` when *settings.enabled* is ``False`` or Playwright is not
        installed.

    """
    if not settings.enabled:
        return []

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "render_url is enabled but playwright is not installed — "
            "install the 'render-url' extra"
        )
        return []

    timeout_ms = settings.timeout * 1000

    async def render_url(url: str, text_only: bool = False) -> Any:
        """Render a URL in headless Chromium and return the page content.

        Loads *url* in a headless Chromium browser, extracts the ARIA
        accessibility tree, and (unless *text_only*) captures a full-page
        screenshot.  Read-only — no clicks, no form fills, no state
        mutation.  The browser is closed immediately after the capture.

        Args:
            url: The fully-qualified http(s) URL to render (e.g.
                ``https://example.com/page``).
            text_only: When ``True``, skip the screenshot and return only
                the textual content (page title, URL, a11y tree) as a JSON
                string.  Defaults to ``False``.

        Returns:
            With a screenshot: a two-part list of a ``TextContent`` holding
            the JSON metadata (``page_title``, ``page_url``,
            ``accessibility_tree``, ``error``) and a ``BinaryContent``
            holding the PNG, which the transport turns into a viewable
            image block.  In *text_only* mode, or when the render failed
            and there is no image, the JSON string alone.

        """
        result: dict[str, Any] = {
            "page_title": "",
            "page_url": url,
            "accessibility_tree": None,
            "error": "",
        }
        screenshot_bytes: bytes | None = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                try:
                    # Attach fleet-auth credentials when the target host
                    # is in the configured auth_hosts list.
                    context_kwargs: dict[str, Any] = {
                        "viewport": {
                            "width": settings.viewport_width,
                            "height": settings.viewport_height,
                        },
                    }
                    context = await browser.new_context(**context_kwargs)
                    page = await context.new_page()

                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )

                    result["page_title"] = await page.title()
                    result["page_url"] = page.url

                    # Full-page screenshot (skipped in text-only mode to
                    # keep the response compact).  Returned as a binary
                    # part, never base64-in-JSON — see the module docstring.
                    if not text_only:
                        raw = await page.screenshot(full_page=True)
                        screenshot_bytes = _downscale_png(raw)

                    # ARIA accessibility tree snapshot (YAML-like string).
                    a11y_snapshot = await page.locator("body").aria_snapshot()
                    if a11y_snapshot:
                        result["accessibility_tree"] = a11y_snapshot

                    await context.close()
                finally:
                    await browser.close()

        except Exception as exc:
            logger.exception("render_url failed for %s", url)
            result["error"] = f"{type(exc).__name__}: {exc}"

        metadata = json.dumps(result, ensure_ascii=False)
        if screenshot_bytes is None:
            return metadata

        from pydantic_ai.messages import BinaryContent, TextContent

        return [
            TextContent(content=metadata),
            BinaryContent(data=screenshot_bytes, media_type="image/png"),
        ]

    return [render_url]
