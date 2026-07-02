# Security Policy

## Supported versions

**robotsix-chat is pre-alpha.** No version has reached a stable release. Security patches are
applied to the `main` branch. There are no backport or LTS branches at this stage.

| Version | Supported          |
| ------- | ------------------ |
| main    | :white_check_mark: |

We target the latest stable Python release (≥ 3.12). Older Python versions are not tested and are
out of scope for vulnerability reports.

## Reporting a vulnerability

**Preferred:** Use [GitHub's private vulnerability reporting][gh-advisory] on this repository. This
keeps the report confidential and allows us to collaborate on a fix before public disclosure.

**Fallback:** If you cannot use GitHub's advisory form, email
[security@robotsix.com][security-email]. Include as much detail as possible: steps to reproduce,
affected versions, and any suggested mitigations.

## Response timeline

| Milestone              | Target                                      |
| ---------------------- | ------------------------------------------- |
| Acknowledgement        | Within 72 hours                             |
| Preliminary assessment | Within 5 business days                      |
| Patch or mitigation    | Depends on severity; we'll keep you updated |

We aim to publish advisories alongside fixes. If you need an embargo extension, let us know in the
report.

## Security model

robotsix-chat is a **same-origin browser + SSE chat server** that wraps an LLM agent and serves it
over HTTP.

**What is in scope:**

- The HTTP endpoints (`GET /`, `POST /chat`, `GET /health`) served by the Starlette application.
- The LLM agent wrapper (`src/robotsix_chat/llm/agent.py`) and its interaction with the `llmio`
  library.
- The configuration layer (`src/robotsix_chat/config/settings.py`) — environment variable handling,
  defaults, and validation.

**What is out of scope:**

- Vulnerabilities in upstream dependencies (`llmio`, `starlette`, `uvicorn`, `pydantic`) that are
  not caused by our usage patterns. Report those to the upstream maintainers.
- Vulnerabilities in the LLM provider's API — those are the provider's responsibility.
- Social-engineering, phishing, or attacks that rely on compromising the operator's machine rather
  than the software itself.

**Known design choices with security implications:**

1. **No built-in auth.** The server binds to `127.0.0.1` by default (localhost-only). Exposing it on
   a public interface is a conscious operator decision. If you do this, put the server behind a
   reverse proxy that handles authentication.
2. **No tool exposure over HTTP.** Agent tools are registered in Python code only — the `/chat`
   endpoint streams LLM tokens and never accepts or executes arbitrary tool definitions from
   clients.
3. **CORS is opt-in.** The `CORS_ALLOW_ORIGINS` environment variable must be explicitly set to
   enable cross-origin requests. By default the UI and API share the same origin.

## Dependency scanning

Dependencies are scanned automatically via:

- **Dependabot** — alerts for known vulnerabilities in direct and transitive dependencies.
- **uv audit** — runs in CI against the project's lockfile on every push to `main`.

We aim to patch or mitigate dependency vulnerabilities within the response timeline above.

[gh-advisory]: https://github.com/robotsix/robotsix-chat/security/advisories/new
[security-email]: mailto:security@robotsix.com
