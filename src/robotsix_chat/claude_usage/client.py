"""ClaudeUsageClient — on-demand Claude.ai remaining-cap scraper.

Drives a headless Chromium browser (Playwright) through Anthropic's email
magic-link login flow, retrieving the one-time login link from the auto-mail
inbox via :class:`~robotsix_chat.mail.client.MailClient`.  Nothing is
persisted: the authenticated session lives only for the duration of a single
:meth:`ClaudeUsageClient.fetch_remaining_cap` call and is discarded when the
browser closes.

Degrades gracefully — Playwright import errors, navigation failures, and a
missing login email all become a diagnostic string in the returned payload
rather than a raised exception.

The scraper is deliberately fragile (see this package's ``README.md``): it
breaks on claude.ai layout changes, CAPTCHA, or device-verification
challenges, and there is no official API backing it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import ClaudeUsageSettings, MailSettings

logger = logging.getLogger(__name__)

#: Matches any absolute claude.ai / anthropic.com URL in an email body.  The
#: magic-link email embeds the one-time login URL as an anchor href and/or as
#: bare text; we grab every candidate and rank them below.
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9.\-]*(?:claude\.ai|anthropic\.com)/[^\s\"'<>)\]]+",
    re.IGNORECASE,
)

#: Path fragments that mark a URL as the actual login/magic link rather than a
#: marketing or help link that also happens to point at claude.ai.
_MAGIC_HINTS = ("magic", "login", "verify", "auth", "sign-in", "signin", "token")


def extract_magic_link(text: str) -> str | None:
    """Return the Anthropic login magic-link URL found in *text*, or ``None``.

    Scans *text* (an email body or the full auto-mail board JSON) for
    absolute claude.ai / anthropic.com URLs and returns the one most likely
    to be the one-time login link — preferring URLs whose path contains a
    magic/login/verify/token hint.  Returns ``None`` when no candidate URL is
    present.
    """
    candidates: list[str] = _URL_RE.findall(text)
    if not candidates:
        return None
    for url in candidates:
        lowered = url.lower()
        if any(hint in lowered for hint in _MAGIC_HINTS):
            return url
    # No hinted URL — fall back to the first claude.ai/anthropic.com link.
    return candidates[0]


def parse_usage_value(text: str) -> str | None:
    """Return a best-effort remaining-cap summary scraped from *text*.

    *text* is the accessibility tree / visible text of the claude.ai usage
    page.  Returns the first line mentioning usage/limit/cap/reset (or a
    percentage / token figure), or ``None`` when nothing matches — the caller
    still returns the raw text so a human can read it when parsing fails.
    """
    if not text:
        return None
    keyword_re = re.compile(
        r"(%|percent|token|message|limit|cap|usage|remaining|reset|weekly)",
        re.IGNORECASE,
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and keyword_re.search(line):
            return line
    return None


#: A realistic desktop-Chrome fingerprint used for the headless session so
#: claude.ai's anti-bot layer is less likely to serve a Cloudflare challenge.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_LOCALE = "en-US"
_TIMEZONE = "America/New_York"
_VIEWPORT = {"width": 1280, "height": 800}
_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

#: Minimal ``navigator.webdriver`` masking + fingerprint smoothing injected
#: before any page script runs.  This is plain evasion, NOT a CAPTCHA solver.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

#: Substrings that mark a rendered page as a Cloudflare bot-verification
#: interstitial (checked against the page title, HTML body, and URL).
_CLOUDFLARE_MARKERS = (
    "just a moment",
    "performing security verification",
    "attention required",
    "challenge-form",
    "cf-chl",
    "__cf_chl",
    "cf-mitigated",
)


def is_cloudflare_challenge(title: str, html: str, url: str) -> bool:
    """Return ``True`` when the page looks like a Cloudflare bot interstitial.

    Detects Anthropic's Cloudflare challenge wall by scanning the page
    *title*, *html* body, and *url* for well-known challenge markers
    ("Just a moment...", ``challenge-form``, ``__cf_chl`` token, etc.).  Used
    so the scraper can return a specific "blocked by Cloudflare" error instead
    of a bare fill/navigation timeout.
    """
    haystacks = ((title or "").lower(), (html or "").lower(), (url or "").lower())
    return any(marker in hay for marker in _CLOUDFLARE_MARKERS for hay in haystacks)


#: Substrings that mark a Cloudflare **Turnstile** widget embedded in the login
#: form itself (as opposed to the full-page interstitial detected above).  An
#: unsatisfied Turnstile blocks the login-email submit, so the magic-link is
#: never sent — a terminal condition for headless automation.
_TURNSTILE_MARKERS = (
    "challenges.cloudflare.com",
    "cf-turnstile",
    "cf-chl-widget",
    "turnstile",
    "challenge-form",
)


def is_turnstile_challenge(html: str) -> bool:
    """Return ``True`` when *html* contains a Cloudflare Turnstile widget.

    Scans the login page markup for the Turnstile iframe/element markers
    (``challenges.cloudflare.com``, ``cf-turnstile``, ``#challenge-form``, …).
    When present at submit time the automated browser cannot satisfy the
    challenge, so Anthropic never sends the magic-link email — the caller
    returns a specific Turnstile error instead of a generic email-timeout.
    """
    lowered = (html or "").lower()
    return any(marker in lowered for marker in _TURNSTILE_MARKERS)


#: Substrings that mark the post-submit "we sent you a login link" confirmation
#: view.  Reaching this state proves the email submit actually triggered
#: Anthropic to send the magic-link, so it gates the start of the mail poll.
_CONFIRMATION_MARKERS = (
    "check your email",
    "check your inbox",
    "we sent",
    "we've sent",
    "sent you a link",
    "sent a link",
    "sent a login link",
    "magic link",
    "verify your email",
    "confirm your email",
)


#: How many times / how long to poll for the post-submit confirmation view
#: before treating the submit as failed.
_CONFIRMATION_POLL_ATTEMPTS = 8
_CONFIRMATION_POLL_INTERVAL = 1.0


def is_login_email_confirmation(title: str, text: str) -> bool:
    """Return ``True`` when the page shows the post-submit "check your email" view.

    Scans the page *title* and visible *text* for confirmation markers
    ("Check your email", "We sent you a link", …).  Used to prove the login
    email was actually submitted before the caller starts polling the inbox.
    """
    haystacks = ((title or "").lower(), (text or "").lower())
    return any(marker in hay for marker in _CONFIRMATION_MARKERS for hay in haystacks)


class ClaudeUsageClient:
    """Headless-browser scraper for the Claude.ai remaining-cap value."""

    def __init__(
        self,
        settings: ClaudeUsageSettings,
        mail_settings: MailSettings,
    ) -> None:
        """Store the scraper config and the auto-mail settings."""
        self._settings = settings
        self._mail_settings = mail_settings

    async def _poll_magic_link(self) -> str | None:
        """Poll the auto-mail board until the Anthropic login link appears.

        Returns the magic-link URL, or ``None`` when it never arrives within
        the configured poll budget.
        """
        from robotsix_chat.mail.client import MailClient

        mail = MailClient(self._mail_settings)
        account_id = self._settings.mail_account_id or None
        for attempt in range(self._settings.mail_poll_attempts):
            board = await mail.board_content(account_id=account_id)
            link = extract_magic_link(board)
            if link:
                return link
            if attempt + 1 < self._settings.mail_poll_attempts:
                await asyncio.sleep(self._settings.mail_poll_interval)
        return None

    async def _await_login_confirmation(self, page: Any) -> bool:
        """Poll the page for the post-submit "check your email" confirmation.

        Returns ``True`` once the confirmation view appears (proving the login
        email was actually sent), or ``False`` when it never appears within a
        bounded number of short polls.
        """
        for attempt in range(_CONFIRMATION_POLL_ATTEMPTS):
            try:
                title = await page.title()
                text = await page.content()
            except Exception:
                title = text = ""
            if is_login_email_confirmation(title, text):
                return True
            if attempt + 1 < _CONFIRMATION_POLL_ATTEMPTS:
                await asyncio.sleep(_CONFIRMATION_POLL_INTERVAL)
        return False

    async def _capture_page_state(self, page: Any) -> str:
        """Return a short ``title + aria-snippet`` description of *page*.

        Used to record exactly where the login flow stopped so the operator
        can diagnose a failed submit from the tool's returned payload.
        """
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            snapshot = await page.locator("body").aria_snapshot()
        except Exception:
            snapshot = ""
        snippet = (snapshot or "").strip()[:400]
        return f"title={title!r} aria={snippet!r}"

    async def fetch_remaining_cap(self) -> dict[str, Any]:
        """Log in via magic-link and scrape the Claude.ai remaining-cap value.

        Orchestrates the full per-call flow: initiate the email magic-link
        login, read the link from the auto-mail inbox, follow it to establish
        a task-scoped session, navigate to the usage page, scrape the value,
        and discard the session.  Never raises — failures become the
        ``error`` field of the returned payload.

        Returns:
            A dict with ``remaining_cap`` (best-effort parsed value or
            ``None``), ``raw_text`` (the scraped page text), ``page_url``
            (final URL), and ``error`` (non-empty on failure).

        """
        result: dict[str, Any] = {
            "remaining_cap": None,
            "raw_text": "",
            "page_url": self._settings.usage_url,
            "error": "",
        }

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            result["error"] = (
                "playwright is not installed — install the 'render-url' extra "
                "to enable the Claude.ai usage scraper"
            )
            return result

        timeout_ms = self._settings.timeout * 1000

        try:
            cf_error = (
                "blocked by Cloudflare bot challenge: claude.ai served a "
                "security-verification interstitial instead of the login form. "
                "Headless-browser login is not viable for this account without "
                "an authenticated-session path; do not add more aggressive "
                "evasion."
            )
            turnstile_error = (
                "blocked by Cloudflare Turnstile on the login form — automated "
                "submit cannot proceed. Anthropic does not send the magic-link "
                "email until the Turnstile challenge is satisfied, which "
                "headless automation cannot do. Pivot to an authenticated-"
                "session path; do not add a CAPTCHA/Turnstile-solving service."
            )
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    # Present a realistic desktop-Chrome fingerprint so the
                    # anti-bot layer is less likely to serve a challenge.
                    context = await browser.new_context(
                        user_agent=_USER_AGENT,
                        locale=_LOCALE,
                        timezone_id=_TIMEZONE,
                        viewport=_VIEWPORT,
                        extra_http_headers={"Accept-Language": _ACCEPT_LANGUAGE},
                    )
                    await context.add_init_script(_STEALTH_INIT_SCRIPT)
                    page = await context.new_page()

                    # 1. Initiate the email magic-link login.
                    await page.goto(
                        self._settings.login_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )

                    # Detect the Cloudflare interstitial up front so the caller
                    # gets a specific error instead of a bare fill timeout.
                    if is_cloudflare_challenge(
                        await page.title(), await page.content(), page.url
                    ):
                        result["error"] = cf_error
                        result["page_url"] = page.url
                        return result

                    # The email field may be gated behind a "Continue with
                    # email" button; click it when present (best-effort).
                    try:
                        continue_btn = page.get_by_role(
                            "button",
                            name=re.compile("continue with email", re.IGNORECASE),
                        )
                        if await continue_btn.count():
                            await continue_btn.first.click()
                    except Exception:
                        logger.debug("claude_usage: no 'Continue with email' button")

                    # Allow challenge/login JS time to render the email field,
                    # with a bounded timeout.
                    email_input = page.locator("input[type='email']").first
                    try:
                        await email_input.wait_for(state="visible", timeout=timeout_ms)
                    except Exception:
                        # Re-check for a challenge that appeared after JS ran;
                        # otherwise report a specific missing-field error.
                        if is_cloudflare_challenge(
                            await page.title(), await page.content(), page.url
                        ):
                            result["error"] = cf_error
                        else:
                            result["error"] = (
                                "claude.ai login email field did not appear "
                                f"within {self._settings.timeout:.0f}s"
                            )
                        result["page_url"] = page.url
                        return result

                    await email_input.fill(self._settings.account_email)

                    # A Cloudflare Turnstile widget on the login form must be
                    # satisfied before Anthropic sends the magic-link email.
                    # The automated browser cannot pass it, so detect it and
                    # return a specific terminal error instead of a generic
                    # "no email arrived" timeout later on.
                    if is_turnstile_challenge(await page.content()):
                        result["error"] = turnstile_error
                        result["page_url"] = page.url
                        return result

                    # 2. Explicitly trigger submission.  Filling the field does
                    # NOT send the request — click the submit / "Continue with
                    # email" control, falling back to an Enter keypress.
                    submitted = False
                    try:
                        submit_btn = page.get_by_role(
                            "button",
                            name=re.compile(
                                "continue|sign in|log in|submit|next|email",
                                re.IGNORECASE,
                            ),
                        )
                        if await submit_btn.count():
                            await submit_btn.first.click()
                            submitted = True
                    except Exception:
                        logger.debug("claude_usage: submit-button click failed")
                    if not submitted:
                        await email_input.press("Enter")

                    # 3. Wait for the post-submit "check your email" confirmation
                    # before polling the inbox — reaching it proves the submit
                    # actually triggered the magic-link send.
                    if not await self._await_login_confirmation(page):
                        page_state = await self._capture_page_state(page)
                        result["raw_text"] = page_state
                        result["page_url"] = page.url
                        # A Turnstile that appeared/gated the submit is the
                        # terminal condition; report it specifically.
                        if is_turnstile_challenge(await page.content()):
                            result["error"] = turnstile_error
                        else:
                            result["error"] = (
                                "claude.ai did not show the 'check your email' "
                                "confirmation after the login-email submit, so "
                                "the magic-link was likely never sent; "
                                f"post-submit page state: {page_state}"
                            )
                        return result

                    # 4. Retrieve the one-time login link from the inbox.
                    link = await self._poll_magic_link()
                    if not link:
                        result["error"] = (
                            "no Anthropic login email arrived in the auto-mail "
                            "inbox within the poll budget"
                        )
                        return result

                    # 5. Follow the link to establish a task-scoped session.
                    await page.goto(
                        link,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )

                    # 6. Navigate to the usage page and scrape the value.
                    await page.goto(
                        self._settings.usage_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    result["page_url"] = page.url
                    snapshot = await page.locator("body").aria_snapshot()
                    raw_text = snapshot or ""
                    result["raw_text"] = raw_text
                    result["remaining_cap"] = parse_usage_value(raw_text)

                    # 7. Discard the session.
                    await context.close()
                finally:
                    await browser.close()
        except Exception as exc:
            logger.exception("claude_usage: fetch_remaining_cap failed")
            result["error"] = f"{type(exc).__name__}: {exc}"

        return result
