# Public Fetch — scoped public-URL fetch tool

You have a `fetch_public_url` tool that performs a single HTTP(S) GET against a public URL, follows
redirects (up to the configured limit), and returns the raw text contents with metadata.

## When to use it

- Read a public repository's README, source file, or raw file content from any public forge (GitHub
  raw, GitLab raw, Bitbucket raw, or any other public URL).
- Fetch reference documentation or portfolio material the operator points you at.
- Inspect a public file whose content you need but that is not in an allowlisted repo.

## Allowed operation

| Tool               | Description                                                   |
| ------------------ | ------------------------------------------------------------- |
| `fetch_public_url` | HTTP(S) GET against a public URL; returns raw text + metadata |

The tool signature is:

```python
fetch_public_url(url: str, cookies: dict[str, str] | None = None) -> str
```

### Parameters

- `url` — the fully-qualified http(s):// URL to fetch.
- `cookies` — optional dictionary of cookie name-value pairs to inject into the request.  Cookies
  are forwarded through redirects.  **WARNING**: cookies may contain session tokens or other
  sensitive credentials — handle with care and never log or expose cookie values.

## Return value

A JSON string with these fields:

- `url` — the original URL you supplied
- `final_url` — the URL after any redirects
- `status_code` — final HTTP status code
- `content_type` — the `Content-Type` response header
- `body_size_bytes` — total response body size (bytes)
- `text` — the raw body text (truncated at the configured cap, default ~1 MB)
- `truncated` — `true` when the body exceeded the size cap and was truncated
- `fetched_at` — ISO-8601 UTC timestamp of the fetch
- `error` — non-empty string when the fetch failed (blocked, timed out, etc.)

## Supported URL shapes

The tool accepts any public HTTP(S) URL, including:

- **GitHub raw**: `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>`
- **GitLab raw** (gitlab.com and self-hosted):
  `https://gitlab.com/<group>/<repo>/-/raw/<branch>/<path>`
- **Bitbucket raw**: `https://bitbucket.org/<workspace>/<repo>/raw/<branch>/<path>`

## Safety

- **Read-only** — GET only; no other HTTP methods.
- **No credentials by default** — the request carries no auth headers or cookies for public URLs.
  Fleet-auth hosts (operator-configured) carry Basic-Auth headers injected server-side — the
  credential is never exposed to you.
- **Cookie injection** — when you supply a `cookies` dictionary, the cookies are injected into the
  request and forwarded through redirects.  **WARNING**: cookies may contain session tokens or other
  sensitive credentials — handle with care and never log or expose cookie values.
- **SSRF protection** — internal/private IP ranges (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
  192.168.0.0/16, 169.254.0.0/16, localhost, IPv6 unique-local/link-local) are blocked.
- **Domain allowlist** — optional, operator-controlled; when empty any public host is allowed.
- **Size-capped** — response body is capped at the configured maximum (default ~1 MB); when
  truncated, `truncated` is `true` and the `body_size_bytes` reports the full size.
- **Timeout** — short per-request timeout (default 10 s).
- **Rate-limited** — configurable sliding-window rate limit (default 10 req/minute).
- **Auth refusal** — HTTP 401/403 responses on non-fleet hosts are reported with a clear error
  message. Fleet-auth hosts have credentials injected server-side and should not see 401/403 for
  auth reasons; if they do, the credentials may need updating.
- **Audited** — every fetch is logged at WARNING level with URL, disposition, status, size, and a
  SHA-256 hash of the response body.

## Example calls

```python
# Fetch a raw README from GitHub
fetch_public_url(
    "https://raw.githubusercontent.com/damien-robotsix/robotsix-standards/main/README.md"
)

# Fetch a raw file from a self-hosted GitLab
fetch_public_url(
    "https://gitlab.univ-nantes.fr/ls2n-drones/ls2n_drone_armada/-/raw/main/README.md"
)

# Fetch public API docs
fetch_public_url("https://docs.python.org/3/library/ipaddress.html")

# Fetch a URL with cookies (e.g., for authenticated endpoints)
fetch_public_url(
    "https://example.com/api/data",
    cookies={"session_id": "abc123", "user_token": "xyz789"}
)
```
