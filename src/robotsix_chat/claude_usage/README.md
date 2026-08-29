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

The `render-url` extra (Playwright + headless Chromium) must be installed. The `mail` integration
must be enabled and reachable so the magic-link email can be read. See
[`docs/configuration.md`](../../../docs/configuration.md) (`### Claude Usage`) for every config key.
The confirmation-gated `fetch_claude_usage` tool is then offered to the chat agent (see
[`skill.md`](skill.md)).
