# Render URL — headless Chromium page capture

You have a `render_url` tool that loads a URL in a headless Chromium browser, captures a full-page
screenshot (PNG, base64-encoded), and extracts the ARIA accessibility tree. Read-only — no clicks,
no form fills, no state mutation.

## When to use it

- Visually inspect what a page actually looks like after rendering — JavaScript, CSS, and layout all
  execute normally.
- Verify that an authenticated fleet UI (e.g. `https://invest.deploy.robotsix.net/docs`) is serving
  the expected content.
- Check the rendered page structure via the accessibility tree when you need a text-only view of the
  page's semantic content.

## Allowed operation

| Tool         | Description                                                              |
| ------------ | ------------------------------------------------------------------------ |
| `render_url` | Load a URL in headless Chromium; return screenshot + accessibility tree. |

The tool signature is:

```python
render_url(
    url: str,
    text_only: bool = False,
) -> str
```

## Return value

A JSON string with these fields:

- `page_title` — the document `<title>`
- `page_url` — the final URL after any redirects
- `screenshot_base64` — full-page PNG as a `data:image/png;base64,…` data URL (empty when
  `text_only` is `True`)
- `accessibility_tree` — the ARIA snapshot as a YAML-like string (`None` when unavailable)
- `error` — non-empty string when the render failed

## Text-only mode

Pass `text_only=True` to omit the screenshot. The response is compact and suitable for subsessions
or contexts that cannot handle large base64 blobs. The accessibility tree is still included.

## Authenticated fleet UIs

When the operator has configured fleet-auth credentials (server-side, never exposed to you), the
browser automatically supplies HTTP basic-auth credentials when the target hostname is in the
configured `fleet_auth.auth_hosts` list.

This means you can render authenticated fleet UIs — the credentials are injected server-side and you
never see them.

## Safety

- **Read-only** — the page is loaded and captured; no clicks, form fills, or navigation.
- **Credentials never exposed** — when fleet-auth is configured, HTTP credentials are injected
  server-side into the browser context; you never see them.
- **Timeout** — the page load has a configurable timeout (default 30 s).
- **Single page** — one URL per call; the browser is closed immediately after capture.
