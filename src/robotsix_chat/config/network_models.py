"""Network Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DockerDigestSettings(BaseModel):
    """Read-only Docker digest resolution tool for the agent.

    When enabled, the agent gains a ``resolve_docker_digest`` tool that
    resolves a Docker image reference (e.g. ``python:3.14-slim``) and
    target platform to its immutable ``sha256:...`` content digest by
    querying the Docker Registry v2 HTTP API.

    Attributes:
        enabled: Master switch.  When ``False``, no docker_digest tool is offered.
        timeout: Per-request HTTP timeout in seconds (default 30 s).
        registry_host: Docker Registry v2 hostname for manifest lookups.
            Default ``registry-1.docker.io`` (Docker Hub).
        auth_url: Token-authentication endpoint for bearer tokens.
            Default ``https://auth.docker.io/token`` (Docker Hub's auth
            service).

    """

    enabled: bool = True
    timeout: float = 30.0
    registry_host: str = "registry-1.docker.io"
    auth_url: str = "https://auth.docker.io/token"
    model_config = ConfigDict(extra="forbid")


class GatewayRouteSettings(BaseModel):
    """Read-only gateway-route diagnostic tool for the agent.

    When enabled, the agent gains a ``check_gateway_route`` tool that reads
    central-deploy's component registry, derives the current
    vhost → upstream mapping, and compares it with the expected route for a
    supplied service slug.  central-deploy publishes every registered,
    routable component at ``<id>.<gateway_base_domain>`` automatically, so a
    route is "present" exactly when the slug is registered.

    Attributes:
        enabled: Master switch.  When ``False``, no check_gateway_route tool
            is offered.
        timeout: Per-request HTTP timeout in seconds (default 30 s).
        gateway_base_domain: Fleet base domain used to derive the expected
            vhost ``<slug>.<base_domain>``.  Must match central-deploy's
            ``gateway_base_domain`` setting.

    """

    enabled: bool = False
    timeout: float = 30.0
    gateway_base_domain: str = "deploy.robotsix.net"
    model_config = ConfigDict(extra="forbid")


class PublicFetchSettings(BaseModel):
    """Scoped public-repo-fetch tool for the chat agent.

    When enabled, the agent gains a ``fetch_public_url`` tool that performs
    a plain HTTP(S) GET to a user-provided public URL, returns the raw
    text/file contents with metadata, and writes an audit-log entry per
    fetch.  SSRF protection blocks internal/private IP ranges for public
    hosts.  Fleet components, resolved from the central-deploy roster, are
    trusted by the operator: they bypass the SSRF check and the domain
    allowlist, and their requests carry server-injected basic auth.

    Attributes:
        enabled: Master switch.  When ``False``, no tool is offered.
        timeout: Per-request HTTP timeout in seconds (default 10 s).
        max_body_bytes: Maximum bytes of the response body to read and
            return to the agent (default 1_048_576 — ~1 MB).
        max_redirects: Maximum number of redirects to follow (default 5).
        domain_allowlist: Optional list of hostnames (no protocol, no
            path) that the tool is permitted to fetch.  When empty, any
            public hostname is allowed (subject to SSRF checks).
        rate_limit_requests: Maximum number of requests allowed within
            ``rate_limit_window_seconds`` (default 10).
        rate_limit_window_seconds: Sliding window in seconds for the
            rate limiter (default 60.0).

    """

    enabled: bool = False
    timeout: float = 10.0
    max_body_bytes: int = 1_048_576
    max_redirects: int = 5
    domain_allowlist: list[str] = Field(default_factory=list)
    rate_limit_requests: int = 10
    rate_limit_window_seconds: float = 60.0
    model_config = ConfigDict(extra="forbid")
