"""On-demand Claude.ai remaining-cap scraper (headless browser + magic-link).

Exposes :func:`build_claude_usage_tools` — a factory returning the
confirmation-gated ``fetch_claude_usage`` tool, and :func:`load_claude_usage_skill`
— the component skill markdown.

The tool drives a headless Chromium browser through Anthropic's email
magic-link login flow (retrieving the one-time link from the auto-mail inbox),
scrapes the Claude.ai usage page for the remaining weekly-cap / token value,
and discards the session — **nothing is persisted, no credentials are
stored**.  Returns no tools when disabled, so the chat runs exactly as before.
"""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import ClaudeUsageSettings, MailSettings

__all__ = ["build_claude_usage_tools", "load_claude_usage_skill"]


def load_claude_usage_skill() -> str:
    """Return the ``skill.md`` content for the Claude-usage tool.

    Returns an empty string when the file is missing or unreadable so a
    missing skill doc never breaks the agent prompt.
    """
    try:
        return (
            importlib.resources.files("robotsix_chat.claude_usage")
            .joinpath("skill.md")
            .read_text()
        )
    except Exception:
        return ""


def build_claude_usage_tools(
    settings: ClaudeUsageSettings,
    mail_settings: MailSettings,
) -> list[Callable[..., Any]]:
    """Return the ``fetch_claude_usage`` tool, or ``[]`` when disabled.

    Args:
        settings: Claude-usage scraper configuration (``enabled`` master
            switch, account email, login/usage URLs, mail-poll budget).
        mail_settings: Auto-mail integration settings, used to read the
            one-time magic-link login email from the inbox.

    Returns:
        A single-element list containing the ``fetch_claude_usage`` async
        callable, or ``[]`` when *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    from .client import ClaudeUsageClient

    client = ClaudeUsageClient(settings, mail_settings)

    async def fetch_claude_usage() -> str:
        """Fetch the current Claude.ai remaining weekly-cap / token value.

        **This is a confirmation-gated read.**  You MUST obtain explicit
        operator approval before calling this function — it initiates a real
        Claude.ai login (Anthropic emails a one-time magic-link) and scrapes
        an authenticated console page.  State clearly to the operator that
        you are about to:

        * Trigger a fresh email magic-link login for the configured
          Claude.ai account (a login email will be sent).
        * Read that one-time login email from the auto-mail inbox.
        * Follow the link, scrape the usage page, and discard the session.

        Wait for a clear confirmation reply (e.g. "yes", "go ahead") before
        proceeding.

        Each call uses a **fresh magic-link login** — no credentials are
        stored, and the authenticated session is discarded immediately after
        scraping.  The scraper is fragile: it can break on claude.ai layout
        changes, CAPTCHA, or device-verification challenges.

        Returns:
            A JSON string with ``remaining_cap`` (best-effort parsed value or
            ``null``), ``raw_text`` (the scraped page text so you can read the
            value yourself when parsing fails), ``page_url``, and ``error``
            (non-empty when the fetch failed).

        Never raises — errors become the ``error`` field of the JSON payload.

        """
        payload = await client.fetch_remaining_cap()
        return json.dumps(payload, ensure_ascii=False)

    return [fetch_claude_usage]
