# claude_usage — Claude.ai remaining-cap scraper

An on-demand **headless-browser automation** component that retrieves the Claude.ai **Team account**
remaining-token / weekly-cap value for the account tied to the fleet (default `chat@robotsix.net`).
This value lives only behind an authenticated web console — there is **no official API** for it — so
this component scrapes it with a headless Chromium browser.

## Design: no stored secrets, fresh login per call

The component holds **no credentials** and provisions **no persistent secret**. On every call it:

1. Initiates a login to claude.ai using Anthropic's **email magic-link** flow (Anthropic emails a
   one-time login link).
1. Retrieves that one-time login email/link via the existing **robotsix-auto-mail** component (reads
   the inbox, extracts the link).
1. Follows the link to establish a **task-scoped** authenticated session.
1. Navigates to the usage/settings page and scrapes the remaining-cap value.
1. Returns the value and **discards the session** — nothing is persisted.

The magic-link email is the sole auth per run: a task-specific one-time instruction, not a stored
secret. There is deliberately **no password / API-key vault path** (an operator may plug a
Vaultwarden account into the loop in a future iteration — out of scope here).

## Session-state reuse mode (`auth_mode: "session_state"`)

Anthropic's login page is intermittently gated by a Cloudflare anti-bot wall that headless
automation cannot pass (and which must **not** be worked around with CAPTCHA/Turnstile-solving —
explicitly out of scope). The durable fix is to skip the login page entirely by reusing an
operator-captured, already-authenticated browser session.

When `auth_mode` is set to `"session_state"`, the component:

1. Loads a Playwright **storage-state** JSON (cookies + localStorage) from `session_state_path`,
   read at call time — the blob never passes through chat and its contents are never logged.
1. Creates the browser context seeded with that state and navigates **directly** to `usage_url`
   (`https://claude.ai/settings/usage`) — no login page, no email, no magic-link poll.
1. Scrapes the remaining-cap value with the same parser and discards the session — nothing new is
   persisted.

Errors are specific and actionable:

- **No session configured** (path unset / file missing / empty / invalid JSON):
  `"no session state configured; operator must capture one …"`.
- **Expired or challenged session** (the usage page redirects to login, or a Cloudflare
  interstitial is served): `"claude.ai session expired or challenged — operator must re-capture the
  browser session state."` — the signal to refresh the cookie, not to debug the code.

### Capturing the storage-state blob (operator procedure)

1. Log into claude.ai in a **normal** desktop browser as the fleet account, and confirm you can view
   `https://claude.ai/settings/usage`.
1. Export the session as a Playwright **storage-state** JSON (cookies + localStorage). The simplest
   route is a one-off Playwright capture, e.g.:

   ```python
   from playwright.sync_api import sync_playwright

   with sync_playwright() as p:
       browser = p.chromium.launch(headless=False)
       context = browser.new_context()
       page = context.new_page()
       page.goto("https://claude.ai/login")
       input("Log in in the opened browser, then press Enter here…")
       context.storage_state(path="claude-session.json")
       browser.close()
   ```

   A documented browser-extension / devtools cookie+localStorage export producing the same
   Playwright storage-state JSON shape works too.
1. Place the resulting JSON file on the config/data volume at the path configured in
   `claude_usage.session_state_path` (e.g. `/data/claude-session.json`).

**Expiry / re-capture cadence.** claude.ai sessions expire after **days to a few weeks**. When the
tool returns the "session expired or challenged" error, re-run the capture above and replace the
file. There is deliberately **no CAPTCHA/Turnstile solving and no credential storage** — the
captured cookie is the sole auth for this mode.

## Known risks

- **Fragile scraper.** Breaks on claude.ai page/layout changes, CAPTCHA, or device-verification
  challenges. The tool returns the raw scraped page text so a human can read the value when
  automatic parsing fails.
- **No official API / Terms-of-Service caveat.** Automated console access is unofficial and may
  brush against Claude.ai's Terms of Service. Surface this caveat when reporting the value.
- **Mail-delivery dependency.** The Anthropic login email must actually arrive in the mailbox that
  the auto-mail integration can read; otherwise the fetch reports a poll-timeout error.

## Activation

Disabled by default (`claude_usage.enabled: false`). To activate, an operator sets in the config
file:

```json
"claude_usage": {
  "enabled": true,
  "account_email": "chat@robotsix.net"
},
"mail": {
  "enabled": true,
  "api_base_url": "http://<auto-mail-host>:8077"
}
```

To use the more reliable **session-state reuse mode** instead (recommended when Cloudflare blocks
the magic-link login), set `auth_mode` and `session_state_path`, and capture the blob as described
above — the `mail` integration is then not required:

```json
"claude_usage": {
  "enabled": true,
  "auth_mode": "session_state",
  "session_state_path": "/data/claude-session.json"
}
```

The `render-url` extra (Playwright + headless Chromium) must be installed. For `magic_link` mode the
`mail` integration must be enabled and reachable so the magic-link email can be read. See
[`docs/configuration.md`](../../../docs/configuration.md) (`### Claude Usage`) for every config key.
The confirmation-gated `fetch_claude_usage` tool is then offered to the chat agent (see
[`skill.md`](skill.md)).
