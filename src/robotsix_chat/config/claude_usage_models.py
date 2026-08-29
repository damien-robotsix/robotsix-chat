"""Claude Usage Settings Models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ClaudeUsageSettings(BaseModel):
    """On-demand Claude.ai remaining-cap scraper via headless browser.

    When enabled, the agent gains a confirmation-gated ``fetch_claude_usage``
    tool that, **per call**, drives a headless Chromium browser through
    Anthropic's email magic-link login flow for the configured account,
    retrieves the one-time login link from the auto-mail inbox (via the
    ``mail`` integration), follows it to establish a **task-scoped**
    authenticated session, scrapes the usage/settings page for the remaining
    weekly-cap / token value, and then discards the session.

    **No credentials are stored.**  The magic-link email is the sole auth per
    run — there is no password or API-key vault path.  The scraper is
    deliberately fragile (see the component README): it breaks on claude.ai
    layout changes, CAPTCHA, or device-verification challenges, and there is
    no official API backing it.

    Requires the ``render-url`` extra (``playwright``) for the headless
    browser and the ``mail`` integration to be enabled and reachable so the
    magic-link email can be read.

    Two authentication modes are supported via ``auth_mode``:

    * ``"magic_link"`` (the default) — the email magic-link flow described
      above.
    * ``"session_state"`` — reuse an operator-captured, already-authenticated
      browser session.  A Playwright storage-state JSON (cookies +
      localStorage) exported from a real logged-in claude.ai browser is loaded
      from ``session_state_path``; the tool navigates **directly** to the usage
      page (no login page, no email, no magic-link poll).  This durably sidesteps
      Cloudflare's anti-bot login wall.  See the component README for the
      capture procedure; captured sessions expire (days to weeks) and must be
      periodically re-captured.

    Attributes:
        enabled: Master switch.  When ``False`` (the default), no
            ``fetch_claude_usage`` tool is offered.
        auth_mode: Authentication strategy.  ``"magic_link"`` (default) uses
            the email magic-link login flow; ``"session_state"`` reuses an
            operator-captured browser session loaded from
            ``session_state_path`` and navigates directly to the usage page.
        session_state_path: Filesystem path (on the config/data volume) to a
            Playwright storage-state JSON captured by the operator from a real
            logged-in claude.ai browser session.  Read at call time when
            ``auth_mode == "session_state"``; the blob is never logged.
        account_email: The Claude.ai account email to log in as.  Anthropic
            sends the one-time magic-link email to this address; it must be a
            mailbox the auto-mail integration can read.
        login_url: Claude.ai login page URL where the email magic-link flow is
            initiated.
        usage_url: Claude.ai usage/settings page URL scraped for the
            remaining-cap value after login.
        mail_account_id: Optional auto-mail ``account_id`` to scope the inbox
            search to.  Empty means the auto-mail server's default account.
        mail_poll_attempts: How many times to poll the auto-mail board for the
            Anthropic login email before giving up.
        mail_poll_interval: Seconds to wait between auto-mail board polls.
        timeout: Per-navigation timeout in seconds for browser page loads.

    """

    enabled: bool = False
    auth_mode: Literal["magic_link", "session_state"] = "magic_link"
    session_state_path: str = ""
    account_email: str = "chat@robotsix.net"
    login_url: str = "https://claude.ai/login"
    usage_url: str = "https://claude.ai/settings/usage"
    mail_account_id: str = ""
    mail_poll_attempts: int = 12
    mail_poll_interval: float = 5.0
    timeout: float = 30.0
    model_config = ConfigDict(extra="forbid")
