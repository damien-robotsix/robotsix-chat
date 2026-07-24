"""Read-only URL rendering via headless Chromium (Playwright).

Returns a screenshot and accessibility tree so the agent can visually
inspect a rendered page.  No state mutation or form submission — strictly
read-only: the page is loaded, a full-page screenshot is captured, the
accessibility snapshot (a11y tree) is extracted, and the browser is
closed immediately.

Requires the ``render-url`` extra (``playwright``) and a Playwright
Chromium browser installation.  When Playwright is not importable the
factory returns an empty list (graceful degradation).
"""

from __future__ import annotations

__all__ = ["build_render_url_tools"]

import base64
import json
import logging
from collections.abc import Callable
from typing import Any

from robotsix_chat.config.models import RenderUrlSettings

logger = logging.getLogger(__name__)


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

    async def render_url(url: str) -> str:
        """Render a URL in headless Chromium and return a screenshot + a11y tree.

        Loads *url* in a headless Chromium browser, captures a full-page
        screenshot (PNG, base64-encoded), extracts the accessibility tree,
        and returns both as a structured JSON text block.  Read-only —
        no clicks, no form fills, no state mutation.  The browser is
        closed immediately after the capture.

        Args:
            url: The fully-qualified http(s) URL to render (e.g.
                ``https://example.com/page``).

        Returns:
            A JSON string with ``page_title``, ``page_url``,
            ``screenshot_base64`` (the full-page PNG as a base64 data URL),
            ``accessibility_tree`` (the a11y snapshot as a nested dict),
            and ``error`` (non-empty on failure).

        """
        result: dict[str, Any] = {
            "page_title": "",
            "page_url": url,
            "screenshot_base64": "",
            "accessibility_tree": None,
            "error": "",
        }

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                try:
                    context = await browser.new_context(
                        viewport={
                            "width": settings.viewport_width,
                            "height": settings.viewport_height,
                        },
                    )
                    page = await context.new_page()

                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )

                    result["page_title"] = await page.title()
                    result["page_url"] = page.url

                    # Full-page screenshot as base64 data URL.
                    screenshot_bytes = await page.screenshot(full_page=True)
                    result["screenshot_base64"] = (
                        "data:image/png;base64,"
                        + base64.b64encode(screenshot_bytes).decode("ascii")
                    )

                    # Accessibility snapshot — Playwright's built-in a11y tree.
                    a11y_snapshot = await page.accessibility.snapshot()
                    if a11y_snapshot is not None:
                        result["accessibility_tree"] = a11y_snapshot

                    await context.close()
                finally:
                    await browser.close()

        except Exception as exc:
            logger.exception("render_url failed for %s", url)
            result["error"] = f"{type(exc).__name__}: {exc}"

        return json.dumps(result, ensure_ascii=False)

    return [render_url]
