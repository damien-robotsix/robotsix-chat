# Claude Usage — on-demand Claude.ai remaining-cap fetch

You have a `fetch_claude_usage` tool that retrieves the current **remaining weekly-cap / token
value** for the Claude.ai account tied to the fleet (default `chat@robotsix.net`). This value lives
only behind an authenticated web console — there is **no official API** — so the tool scrapes it via
a headless browser.

## How it works (per call)

1. Initiates a login to claude.ai using Anthropic's **email magic-link** flow (Anthropic emails a
   one-time login link).
1. Reads that one-time login email from the **auto-mail** inbox and extracts the link.
1. Follows the link to establish a **task-scoped** authenticated session.
1. Navigates to the usage/settings page and scrapes the remaining-cap value.
1. Returns the value and **discards the session** — nothing is persisted.

## Confirmation gate — REQUIRED

`fetch_claude_usage` is **confirmation-gated**. You MUST obtain explicit operator approval before
calling it. Before calling, tell the operator you are about to:

- Trigger a fresh email magic-link login for the Claude.ai account (a login email will be sent).
- Read that one-time login email from the auto-mail inbox.
- Follow the link, scrape the usage page, and discard the session.

Wait for a clear confirmation reply (e.g. "yes", "go ahead") before proceeding.

## Allowed operation

| Tool                 | Description                                                        |
| -------------------- | ------------------------------------------------------------------ |
| `fetch_claude_usage` | Fresh magic-link login → scrape the Claude.ai remaining-cap value. |

The tool signature is:

```python
fetch_claude_usage() -> str
```

## Return value

A JSON string with:

- `remaining_cap` — best-effort parsed remaining-cap line, or `null` when parsing failed
- `raw_text` — the scraped usage-page text (read this yourself when `remaining_cap` is `null`)
- `page_url` — the final URL after login
- `error` — non-empty string when the fetch failed

## Fragility and caveats

- **No stored credentials.** The magic-link email is the sole auth per run — there is no password or
  API-key vault. Each call performs a fresh login.
- **Fragile scraper.** It breaks on claude.ai page/layout changes, CAPTCHA, or device-verification
  challenges. Relay `raw_text` to the operator when `remaining_cap` is `null`.
- **No official API / ToS caveat.** Automated console access may brush against Claude.ai's Terms of
  Service — surface this caveat when reporting the value.
- **Depends on mail delivery.** The Anthropic login email must actually arrive in the mailbox the
  auto-mail integration can read; if it does not, the tool reports a poll-timeout error.
