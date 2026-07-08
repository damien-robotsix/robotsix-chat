"""Headless Chromium page rendering tool for the agent.

Loads a URL in headless Chromium (Playwright) and returns a screenshot plus
the accessibility tree.  Read-only — no forms are submitted, no state is
mutated.

Exposes :func:`build_render_url_tools` — a factory returning the LLM
tool(s) that let the chat agent visually inspect web pages.  Returns no
tools when disabled, so the chat runs exactly as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import RenderUrlSettings

logger = logging.getLogger(__name__)

__all__ = ["build_render_url_tools"]


def build_render_url_tools(
    settings: RenderUrlSettings,
) -> list[Callable[..., Any]]:
    """Return the render-url tool, or ``[]`` when disabled or unavailable."""
    if not settings.enabled:
        return []

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "render_url is enabled but playwright is not installed — "
            "tool will not be available. Install with: pip install playwright"
        )
        return []

    viewport_width = settings.viewport_width
    viewport_height = settings.viewport_height
    timeout_ms = settings.timeout * 1000  # Playwright uses milliseconds

    async def render_url(url: str) -> list[Any]:
        """Load a URL in headless Chromium and return a screenshot + accessibility tree.

        Use this to visually inspect a web page. It returns:
        1. A screenshot (PNG image) you can visually inspect.
        2. The accessibility (a11y) tree as text for structural checks
           (element presence, visibility, text content).

        This is READ-ONLY: no forms are submitted, no state is mutated.
        Use it to verify UI fixes, inspect page state, or confirm rendered
        output.

        Args:
            url: The http(s) URL to load and render.

        Returns:
            A list containing the a11y tree text and a PNG screenshot image.

        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": viewport_width, "height": viewport_height}
                )
                await page.goto(url, timeout=timeout_ms, wait_until="networkidle")

                # Snapshot accessibility tree
                a11y_snapshot: dict[str, Any] | None = (
                    await page.accessibility.snapshot()  # type: ignore[attr-defined]
                )
                a11y_text = _format_a11y_tree(a11y_snapshot)

                # Take screenshot
                screenshot_bytes = await page.screenshot(type="png", full_page=False)
            finally:
                await browser.close()

        from pydantic_ai.messages import BinaryContent

        return [
            f"Rendered {url}\n\nAccessibility tree:\n{a11y_text}",
            BinaryContent(data=screenshot_bytes, media_type="image/png"),
        ]

    return [render_url]


def _format_a11y_tree(node: dict[str, Any] | None, indent: int = 0) -> str:
    """Format an accessibility tree snapshot into a readable text tree."""
    if node is None:
        return "(empty)"

    lines: list[str] = []
    _format_node(node, indent, lines)
    return "\n".join(lines)


def _format_node(node: dict[str, Any], indent: int, lines: list[str]) -> None:
    """Recursively format a single a11y node."""
    role = node.get("role", "unknown")
    name = node.get("name", "")
    value = node.get("value", "")
    desc = node.get("description", "")

    prefix = "  " * indent
    parts = [role]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f"={value}")
    if desc:
        parts.append(f"({desc})")

    lines.append(prefix + " ".join(parts))

    for child in node.get("children", []) or []:
        _format_node(child, indent + 1, lines)
