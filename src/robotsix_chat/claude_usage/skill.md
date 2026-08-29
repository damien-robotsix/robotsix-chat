# Claude Usage — on-demand Claude.ai remaining-cap fetch

You have a `fetch_claude_usage` tool that retrieves the current **remaining weekly-cap / token
value** for the Claude.ai account tied to the fleet (default `chat@robotsix.net`). This value lives
only behind an authenticated web console — there is **no official API** — so the tool scrapes it via
a headless browser.

## How it works (per call)

The behaviour depends on the configured `claude_usage.auth_mode`:

### `magic_link` mode (default)

1. Initiates a login to claude.ai using Anthropic's **email magic-link** flow (Anthropic emails a
   one-time login link).
1. Reads that one-time login email from the **auto-mail** inbox and extracts the link.
1. Follows the link to establish a **task-scoped** authenticated session.
1. Navigates to the usage/settings page and scrapes the remaining-cap value.
1. Returns the value and **discards the session** — nothing is persisted.

### `session_state` mode

1. Loads an operator-captured Playwright **storage-state** JSON (cookies + localStorage) from
   `claude_usage.session_state_path` — no login page, no email, no magic-link poll.
1. Navigates **directly** to the usage page with that already-authenticated session and scrapes the
   remaining-cap value.
1. Returns the value and **discards the session** — nothing new is persisted.

The operator captures the storage-state blob by logging into claude.ai in a normal browser and
exporting cookies + localStorage as a Playwright storage-state JSON (see the component `README.md`
for the exact procedure). Sessions expire (days–weeks) and must be periodically re-captured. There
is **no CAPTCHA/Turnstile solving and no credential storage**.

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
- `error` — non-empty string when the fetch failed; includes specific terminal-condition messages
  - **Turnstile gate:** `"blocked by Cloudflare Turnstile..."` when Anthropic's login form carries
    an unsatisfied Cloudflare Turnstile challenge. Headless automation cannot satisfy it, so the
    tool stops. This is a terminal condition — do **not** retry.
  - **No confirmation:** when the login-email submit did not reach the "check your email"
    confirmation state (page state is captured in `raw_text`)
  - **No session state (session_state mode):** `"no session state configured; operator must capture
    one …"` when `session_state_path` is unset or the file is missing/empty. The operator must
    capture a session — see the component README.
  - **Session expired (session_state mode):** `"claude.ai session expired or challenged — operator
    must re-capture the browser session state."` when the usage page redirects to login or a
    Cloudflare interstitial is served. This is the signal to refresh the captured cookie, not to
    debug the code. Do **not** retry until the operator re-captures.

## Fragility and caveats

- **No stored credentials.** The magic-link email is the sole auth per run — there is no password or
  API-key vault. Each call performs a fresh login.
- **Cloudflare Turnstile gate.** Anthropic sometimes gates the login form with a Cloudflare
  Turnstile challenge. Headless automation cannot satisfy it — the tool returns a specific
  `"blocked by Cloudflare Turnstile..."` error. This is a **terminal condition**: the tool cannot
  proceed without human intervention or an authenticated-session path. Do not retry.
- **Fragile scraper.** It breaks on claude.ai page/layout changes, CAPTCHA, or device-verification
  challenges. Relay `raw_text` to the operator when `remaining_cap` is `null`.
- **No official API / ToS caveat.** Automated console access may brush against Claude.ai's Terms of
  Service — surface this caveat when reporting the value.
- **Depends on mail delivery.** The Anthropic login email must actually arrive in the mailbox the
  auto-mail integration can read; if it does not, the tool reports a poll-timeout error.
