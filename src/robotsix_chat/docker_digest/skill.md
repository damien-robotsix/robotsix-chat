# Docker Digest — read-only tag→digest resolution tool

You have a `resolve_docker_digest` tool that resolves a Docker image reference (e.g. `python:3.14-slim`, `ghcr.io/damien-robotsix/robotsix-chat:main`) plus a target platform to its immutable `sha256:...` content digest by querying the Docker Registry v2 HTTP API.

## When to use it

- Pinning a Docker base image by SHA256 digest for supply-chain hardening (e.g. replacing `python:3.14-slim` with `python:3.14-slim@sha256:...` in a Dockerfile).
- Verifying that a specific tag points to the digest you expect.
- Resolving a multi-arch manifest list to a platform-specific digest.

## Allowed operation

| Tool                    | Description                                              |
| ----------------------- | -------------------------------------------------------- |
| `resolve_docker_digest` | Resolve an image tag to its immutable content digest     |

The tool signature is:

```python
resolve_docker_digest(image: str, platform: str = "linux/amd64") -> str
```

## Parameters

- `image` — The Docker image reference. Accepts:
  - `image:tag` (e.g. `python:3.14-slim`, `nginx:alpine`)
  - `registry/image:tag` (e.g. `ghcr.io/owner/repo:main`)
  - Bare `image` (defaults to `:latest`)
- `platform` — The target platform in `os/arch` format (default `linux/amd64`). Examples: `linux/amd64`, `linux/arm64`, `linux/arm/v7`.

## Return value

A JSON string with these fields:

- `image` — the original image reference you supplied
- `platform` — the requested platform
- `digest` — the resolved `sha256:...` string, or empty on error
- `resolved_ref` — convenience string `repo:tag@sha256:...` ready to paste into a Dockerfile `FROM` line, or empty on error
- `media_type` — the Docker media type of the resolved manifest (e.g. `application/vnd.docker.distribution.manifest.v2+json`)
- `error` — non-empty human-readable string on any failure, empty on success

## Safety

- **Read-only** — only HTTP GET requests to the Docker Registry v2 API; no writes, pushes, or mutations.
- **No credentials** — uses anonymous/public token exchange only; no Docker credentials are sent.
- **Public registries** — resolves against Docker Hub and public GHCR; private registries will return an authentication error.
- **One request per call** — at most 2–3 HTTP round-trips (token + manifest, possibly + manifest-list sub-manifest).
- **Timeout** — the request has a configurable timeout (default 30 s).

## Example calls

```python
# Pin a Python base image
resolve_docker_digest("python:3.14-slim")
# → {"image": "python:3.14-slim", "platform": "linux/amd64", "digest": "sha256:a1b2...", ...}

# Resolve a specific platform
resolve_docker_digest("python:3.14-slim", platform="linux/arm64")

# Resolve a GHCR image
resolve_docker_digest("ghcr.io/damien-robotsix/robotsix-chat:main")
```
