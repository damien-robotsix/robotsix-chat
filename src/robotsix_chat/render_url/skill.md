# Render URL — headless Chromium page capture

You have a `render_url` tool that loads a URL in a headless Chromium browser, captures a full-page
screenshot you can actually **see**, and extracts the ARIA accessibility tree. Read-only — no
clicks, no form fills, no state mutation.

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

Two parts: a **viewable image** of the rendered page, plus a text part holding JSON metadata:

- `page_title` — the document `<title>`
- `page_url` — the final URL after any redirects
- `accessibility_tree` — the ARIA snapshot as a YAML-like string (`None` when unavailable)
- `error` — non-empty string when the render failed

The screenshot arrives as a real image block, so you can read the page visually — layout, charts,
rendered state. It is capped at ~750k pixels; a very long page is downscaled, so use the
accessibility tree when you need exact fine print.

When the render fails there is no image and you get the JSON metadata alone, with `error` set.

## Text-only mode

Pass `text_only=True` to omit the screenshot and get the JSON metadata alone. Use it when you only
need the page's semantic content — it is markedly cheaper than shipping an image.

## Authenticated fleet UIs

Fleet components are reached at their internal addresses from the central-deploy roster, which needs
no credential. A public `*.deploy.robotsix.net` URL will land on the SSO login page instead of the
component.

This means you can render authenticated fleet UIs — the credentials are injected server-side and you
never see them.

## Safety

- **Read-only** — the page is loaded and captured; no clicks, form fills, or navigation.
- **No credentials at all** — internal addresses need none; nothing is injected server-side into the
  browser context; you never see them.
- **Timeout** — the page load has a configurable timeout (default 30 s).
- **Single page** — one URL per call; the browser is closed immediately after capture.
