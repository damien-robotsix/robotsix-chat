"""Read-only URL rendering via headless Chromium (Playwright).

Returns a screenshot and ARIA accessibility tree so the agent can visually
inspect a rendered page.  No state mutation or form submission — strictly
read-only: the page is loaded, a full-page screenshot is captured, the
ARIA snapshot (a11y tree) is extracted, and the browser is
closed immediately.

Requires the ``render-url`` extra (``playwright``) and a Playwright
Chromium browser installation.  When Playwright is not importable the
factory returns an empty list (graceful degradation).
"""

from __future__ import annotations

__all__ = ["build_render_url_tools", "load_render_url_skill"]

import base64
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from robotsix_chat.config.models import RenderUrlSettings

logger = logging.getLogger(__name__)


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

    async def render_url(url: str, text_only: bool = False) -> str:
        """Render a URL in headless Chromium and return the page content.

        Loads *url* in a headless Chromium browser, extracts the
        ARIA accessibility tree, and optionally captures a full-page screenshot
        (PNG, base64-encoded).  Read-only — no clicks, no form fills, no
        state mutation.  The browser is closed immediately after the
        capture.

        When *text_only* is ``True`` the screenshot is omitted, producing
        a compact response suitable for subsessions that lack file-slicing
        tools and cannot handle large base64 blobs.

        Args:
            url: The fully-qualified http(s) URL to render (e.g.
                ``https://example.com/page``).
            text_only: When ``True``, skip the full-page screenshot and
                return only the textual content (page title, URL, a11y
                tree).  Defaults to ``False``.

        Returns:
            A JSON string with ``page_title``, ``page_url``,
            ``screenshot_base64`` (empty when *text_only* is ``True``,
            otherwise the full-page PNG as a base64 data URL),
            ``accessibility_tree`` (the ARIA snapshot as a YAML-like string),
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

                    # Full-page screenshot as base64 data URL (skipped in
                    # text-only mode to keep the response compact).
                    if not text_only:
                        screenshot_bytes = await page.screenshot(
                            full_page=True,
                        )
                        result["screenshot_base64"] = (
                            "data:image/png;base64,"
                            + base64.b64encode(screenshot_bytes).decode(
                                "ascii",
                            )
                        )

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

        return json.dumps(result, ensure_ascii=False)

    return [render_url]
