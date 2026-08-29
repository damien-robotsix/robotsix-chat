"""Tests for the claude_usage component.

Covers the pure helpers (:func:`extract_magic_link`, :func:`parse_usage_value`),
the tool factory (:func:`build_claude_usage_tools`), and the end-to-end
``fetch_claude_usage`` flow with a mocked Playwright browser chain and a mocked
auto-mail inbox — no real browser or network is ever used.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.claude_usage import build_claude_usage_tools, load_claude_usage_skill
from robotsix_chat.claude_usage.client import (
    extract_magic_link,
    is_cloudflare_challenge,
    parse_usage_value,
)
from robotsix_chat.config import ClaudeUsageSettings, MailSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**kw: Any) -> ClaudeUsageSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return ClaudeUsageSettings(**base)


def _fake_playwright_module(
    usage_text: str,
    *,
    title: str = "Claude",
    content: str = "<html><body>login</body></html>",
) -> Any:
    """Return a mock ``playwright.async_api`` module with a fake browser chain.

    A single locator mock serves both call sites: ``.locator("input...").first``
    (with ``fill`` / ``press`` / ``wait_for``) and
    ``.locator("body").aria_snapshot()``.  ``title``/``content`` drive the
    Cloudflare-interstitial detection branch.
    """
    mock_locator = MagicMock()
    mock_locator.aria_snapshot = AsyncMock(return_value=usage_text)
    mock_locator.fill = AsyncMock()
    mock_locator.press = AsyncMock()
    mock_locator.wait_for = AsyncMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.click = AsyncMock()
    mock_locator.first = mock_locator

    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.url = "https://claude.ai/settings/usage"
    mock_page.title = AsyncMock(return_value=title)
    mock_page.content = AsyncMock(return_value=content)
    mock_page.locator = MagicMock(return_value=mock_locator)
    mock_page.get_by_role = MagicMock(return_value=mock_locator)

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.add_init_script = AsyncMock()
    mock_context.close = AsyncMock()

    mock_browser = MagicMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_chromium = MagicMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw = MagicMock()
    mock_pw.chromium = mock_chromium

    mock_async_pw = MagicMock()
    mock_async_pw.__aenter__ = AsyncMock(return_value=mock_pw)
    mock_async_pw.__aexit__ = AsyncMock(return_value=None)

    module = MagicMock()
    module.async_playwright = MagicMock(return_value=mock_async_pw)
    module._test_page = mock_page
    return module


def _install_fake_playwright(usage_text: str, **kw: Any) -> Any:
    """Inject a fake ``playwright.async_api`` into ``sys.modules``."""
    fake = _fake_playwright_module(usage_text, **kw)
    if "playwright" not in sys.modules:
        sys.modules["playwright"] = MagicMock()
    sys.modules["playwright.async_api"] = fake
    return fake


def _remove_fake_playwright() -> None:
    sys.modules.pop("playwright.async_api", None)


# ---------------------------------------------------------------------------
# extract_magic_link
# ---------------------------------------------------------------------------


def test_extract_magic_link_prefers_hinted_url() -> None:
    """A URL whose path hints login/magic/verify is preferred."""
    text = (
        "Marketing link: https://claude.ai/pricing here.\n"
        "Log in: https://claude.ai/magic-link?token=abc123 now."
    )
    assert extract_magic_link(text) == "https://claude.ai/magic-link?token=abc123"


def test_extract_magic_link_falls_back_to_first() -> None:
    """With no hinted URL, the first claude.ai/anthropic.com link is returned."""
    text = "See https://claude.ai/some/page for details."
    assert extract_magic_link(text) == "https://claude.ai/some/page"


def test_extract_magic_link_none_when_absent() -> None:
    """No claude.ai/anthropic.com URL → None."""
    assert extract_magic_link("no relevant links https://example.com/x") is None


# ---------------------------------------------------------------------------
# parse_usage_value
# ---------------------------------------------------------------------------


def test_parse_usage_value_matches_keyword_line() -> None:
    """The first line mentioning a usage keyword is returned."""
    text = "Header\nWeekly limit: 42% remaining\nFooter"
    assert parse_usage_value(text) == "Weekly limit: 42% remaining"


def test_parse_usage_value_none_when_no_match() -> None:
    """No keyword line → None."""
    assert parse_usage_value("just\nsome\nplain text") is None


def test_parse_usage_value_empty() -> None:
    """Empty text → None."""
    assert parse_usage_value("") is None


# ---------------------------------------------------------------------------
# is_cloudflare_challenge
# ---------------------------------------------------------------------------


def test_is_cloudflare_challenge_detects_title() -> None:
    """The 'Just a moment...' title marks a Cloudflare interstitial."""
    assert is_cloudflare_challenge(
        "Just a moment...", "<html></html>", "https://claude.ai/login"
    )


def test_is_cloudflare_challenge_detects_body_and_token() -> None:
    """Challenge-form markup and the __cf_chl URL token are both detected."""
    assert is_cloudflare_challenge("", "<form id='challenge-form'>", "")
    assert is_cloudflare_challenge("", "", "https://claude.ai/login?__cf_chl_rt_tk=abc")


def test_is_cloudflare_challenge_negative() -> None:
    """A normal login page is not flagged as a challenge."""
    assert not is_cloudflare_challenge(
        "Claude", "<input type='email'>", "https://claude.ai/login"
    )


# ---------------------------------------------------------------------------
# ClaudeUsageSettings / skill
# ---------------------------------------------------------------------------


def test_claude_usage_settings_defaults() -> None:
    """Default ClaudeUsageSettings has sensible values and is disabled."""
    s = ClaudeUsageSettings()
    assert s.enabled is False
    assert s.account_email == "chat@robotsix.net"
    assert s.login_url == "https://claude.ai/login"
    assert s.usage_url == "https://claude.ai/settings/usage"
    assert s.mail_poll_attempts == 12


def test_load_claude_usage_skill_nonempty() -> None:
    """The skill markdown is shipped and mentions the tool + confirmation gate."""
    skill = load_claude_usage_skill()
    assert "fetch_claude_usage" in skill
    assert "onfirmation" in skill


# ---------------------------------------------------------------------------
# build_claude_usage_tools
# ---------------------------------------------------------------------------


def test_build_claude_usage_tools_disabled() -> None:
    """Disabled claude_usage returns no tools."""
    assert (
        build_claude_usage_tools(ClaudeUsageSettings(enabled=False), MailSettings())
        == []
    )


def test_build_claude_usage_tools_returns_one_tool() -> None:
    """Enabled claude_usage returns a single tool named fetch_claude_usage."""
    tools = build_claude_usage_tools(_settings(), MailSettings())
    assert len(tools) == 1
    assert tools[0].__name__ == "fetch_claude_usage"


# ---------------------------------------------------------------------------
# fetch_claude_usage — flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_claude_usage_playwright_not_installed() -> None:
    """When playwright is not importable, the payload carries an install hint."""
    _remove_fake_playwright()
    sys.modules.pop("playwright", None)
    tools = build_claude_usage_tools(_settings(), MailSettings())
    payload = json.loads(await tools[0]())
    assert payload["remaining_cap"] is None
    assert "playwright" in payload["error"]


@pytest.mark.asyncio
async def test_fetch_claude_usage_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full magic-link → scrape flow returns the parsed remaining-cap value."""
    _install_fake_playwright("Weekly usage: 73% of cap remaining")
    monkeypatch.setattr(
        "robotsix_chat.mail.client.MailClient.board_content",
        AsyncMock(return_value="Login here: https://claude.ai/magic-link?token=xyz"),
    )
    try:
        tools = build_claude_usage_tools(_settings(), MailSettings())
        payload = json.loads(await tools[0]())
        assert payload["error"] == ""
        assert payload["remaining_cap"] == "Weekly usage: 73% of cap remaining"
        assert payload["page_url"] == "https://claude.ai/settings/usage"
    finally:
        _remove_fake_playwright()


@pytest.mark.asyncio
async def test_fetch_claude_usage_no_magic_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no login email arrives, the payload reports a poll-timeout error."""
    _install_fake_playwright("irrelevant usage text")
    monkeypatch.setattr(
        "robotsix_chat.mail.client.MailClient.board_content",
        AsyncMock(return_value="empty inbox, no links here"),
    )
    monkeypatch.setattr("robotsix_chat.claude_usage.client.asyncio.sleep", AsyncMock())
    try:
        tools = build_claude_usage_tools(
            _settings(mail_poll_attempts=2), MailSettings()
        )
        payload = json.loads(await tools[0]())
        assert payload["remaining_cap"] is None
        assert "login email" in payload["error"]
    finally:
        _remove_fake_playwright()


@pytest.mark.asyncio
async def test_fetch_claude_usage_cloudflare_interstitial() -> None:
    """A Cloudflare challenge yields a specific error, not a fill timeout."""
    _install_fake_playwright(
        "irrelevant",
        title="Just a moment...",
        content="<form id='challenge-form'>Performing security verification</form>",
    )
    try:
        tools = build_claude_usage_tools(_settings(), MailSettings())
        payload = json.loads(await tools[0]())
        assert payload["remaining_cap"] is None
        assert "Cloudflare" in payload["error"]
    finally:
        _remove_fake_playwright()
