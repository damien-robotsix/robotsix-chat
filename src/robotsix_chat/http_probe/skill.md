# HTTP Probe — read-only uptime / render-probe tool

You have an `http_probe` tool that performs a single HTTPS GET against a public URL, follows
redirects (up to the configured limit), and returns a structured health report.

## When to use it

- Verify that a live website is actually serving content after a deploy (deploy success ≠ the site
  renders — the server could return a directory listing, a maintenance page, or "Not Found").
- Confirm that a service endpoint is reachable from the outside and returning the expected HTTP
  status.
- Check that a page contains (or does NOT contain) specific text — e.g. confirm the expected
  `<title>` appears, or confirm an error string (`"Index of /"`,
  `"Error establishing a database connection"`) is absent.

## Allowed operation

| Tool         | Description                                                          |
| ------------ | -------------------------------------------------------------------- |
| `http_probe` | HTTPS GET against a public URL; returns status, body snippet, health |

The tool signature is:

```python
http_probe(
    url: str,
    expect_status: int = 200,
    expect_contains: list[str] = [],
    expect_absent: list[str] = [],
) -> str
```

## Return value

A JSON string with these fields:

- `url` — the original URL you supplied
- `final_url` — the URL after any redirects
- `status_code` — final HTTP status code
- `response_time_ms` — round-trip time in milliseconds
- `content_type` — the `Content-Type` response header
- `body_size_bytes` — total response body size (bytes)
- `body_snippet` — first ~2 KB of the body text
- `healthy` — `true` when **all** assertions pass *and* no transport error occurred
- `checks` — list of assertion results, each with `check`, `passed`, `detail`
- `error` — non-empty string when a transport/hostname/scheme error prevented the probe

## Assertions

You supply three optional assertion parameters:

- `expect_status` (default 200) — the expected HTTP status
- `expect_contains` — list of substrings that MUST appear in the response body (case-insensitive)
- `expect_absent` — list of substrings that must NOT appear in the response body (case-insensitive)

Every assertion that fails adds a `checks` entry with `passed: false` and a human-readable `detail`.
`healthy` is `false` when any check fails.

Common `expect_absent` patterns:

- `"Index of /"` — Apache/Nginx directory listing
- `"Not Found"` — generic 404 page
- `"Error establishing a database connection"` — WordPress DB down
- `"403 Forbidden"` — access denied
- `"Service Unavailable"` — 503 gateway

## Safety

- **Read-only** — GET only; no other HTTP methods are exposed.
- **No credentials** — the request carries no auth headers or cookies.
- **Hostname allowlisted** — the probe only reaches hosts in a configurable allowlist
  (operator-controlled). At minimum `www.robotsix.net` and `robotsix.net` are permitted.
- **Size-capped** — only the first ~2 KB of the response body are read and returned.
- **Timeout** — the request has a short timeout (default 10 s); one request per call.
- **Internal hosts unreachable** — by default only public-internet hostnames that are explicitly
  allowlisted are reachable; internal fleet hosts are not in the allowlist and are blocked.

## Example calls

```python
# Basic health check — is the site up?
http_probe("https://www.robotsix.net")

# Deploy verification — did the page land?
http_probe("https://www.robotsix.net", expect_status=200,
           expect_contains=["Robotsix"], expect_absent=["Index of /", "Not Found"])
```
