"""Render Url Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RenderUrlSettings(BaseModel):
    """Read-only URL rendering with headless Chromium (Playwright).

    When enabled, the agent gains a tool that loads a URL in a headless
    Chromium browser (via Playwright), takes a full-page screenshot, and
    extracts the ARIA accessibility tree — both returned as structured output.
    No interactive browsing, form-filling, or navigation beyond the initial
    page load is permitted.

    Attributes:
        enabled: Master switch.  When ``False``, no URL-render tool is offered.
        timeout: Per-request timeout in seconds for the page load.
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.

    """

    enabled: bool = True
    timeout: float = 30.0
    viewport_width: int = 1280
    viewport_height: int = 720
    model_config = ConfigDict(extra="forbid")


class HttpProbeSettings(BaseModel):
    """Read-only HTTP uptime/render-probe tool for the agent.

    When enabled, the agent gains an ``http_probe`` tool that performs a
    plain HTTPS GET to a public URL (follows redirects, short timeout)
    and returns the HTTP status, final URL, response time, Content-Type,
    response size, and a snippet of the body text with optional content
    assertions.

    Attributes:
        enabled: Master switch.  When ``False``, no http_probe tool is offered.
        timeout: Per-request HTTP timeout in seconds (default 10 s).
        allowlist: Hostnames (no protocol, no path) that the tool is permitted to
            probe.  At minimum must include ``www.robotsix.net`` and
            ``robotsix.net``.  When empty, the tool permits any public hostname.
        max_body_bytes: Maximum bytes of the response body to read and
            return to the agent (default 2048 — ~2 KB).
        max_redirects: Maximum number of redirects to follow (default 5).

    """

    enabled: bool = True
    timeout: float = 10.0
    allowlist: list[str] = Field(
        default_factory=lambda: ["www.robotsix.net", "robotsix.net"]
    )
    max_body_bytes: int = 2048
    max_redirects: int = 5
    model_config = ConfigDict(extra="forbid")
