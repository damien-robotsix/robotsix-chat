Serve the same session list from every access point. The UI minted a random
per-browser id into `localStorage` and used it as the `owner_id` scoping every
session, so opening the chat on a second computer (or in a private window, or
after clearing site data) created a brand-new owner: the session list came back
empty and — because the list endpoint lazily creates a default session for an
unknown owner — it looked like a populated board rather than an error. This
deployment is single-user, so there is no per-browser identity any more: the UI
sends a fixed owner and the server canonicalises every client-supplied
`owner_id` to one operator pool, which also fixes stale cached copies of the
page. Owner records already on disk are merged into that pool on load (sessions
unioned, the most recently active pointer winning), so no history is lost. The
autonomous runner's reserved owners (`autonomous`, `autonomous:<definition>`)
keep their own pool and are still listed separately.
