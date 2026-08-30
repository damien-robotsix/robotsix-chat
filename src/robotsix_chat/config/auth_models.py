"""Mobile SSO authentication settings models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class MobileAuthSettings(BaseModel):
    """Mobile SSO authentication via tinyauth reverse proxy.

    When enabled, two endpoints are exposed for the mobile app's
    authentication flow:

    - ``GET /auth/login`` — initiates the tinyauth redirect handshake and
      redirects back to the app via a validated deep-link allowlist.
    - ``POST /chat/auth/mobile-token`` — exchanges the tinyauth edge-header
      identity for a short-lived bearer token the mobile app uses for API
      calls.

    The identity is **always** taken from the tinyauth edge header
    (``subject_header``) — never from the request body.  This prevents
    subject spoofing: only the trusted reverse proxy can set the header.

    Attributes:
        enabled: Master switch.  Default ``False``.
        tinyauth_url: Base URL of the tinyauth instance
            (e.g. ``"https://auth.robotsix.net"``).
        subject_header: HTTP header where tinyauth writes the
            authenticated user identity (username or email).
        session_header: HTTP header where tinyauth writes the session
            identifier.
        token_secret: HMAC secret used to sign the short-lived bearer
            tokens issued by ``/chat/auth/mobile-token``.  Must be set
            when *enabled* is ``true``.
        token_ttl_seconds: Bearer token lifetime in seconds.
            Must be > 0.
        allowed_redirect_domains: Allowlist of domains that the
            ``redirect_to`` query parameter in ``GET /auth/login`` is
            validated against.  Must be non-empty when *enabled* is
            ``true``.
        callback_base_url: The public base URL of this chat server
            (e.g. ``"https://chat.robotsix.net"``), used to construct
            the tinyauth callback URL.  Must be set when *enabled* is
            ``true``.

    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    tinyauth_url: str = ""
    subject_header: str = "X-Forwarded-User"
    session_header: str = "X-Forwarded-Session"
    token_secret: SecretStr = SecretStr("")
    token_ttl_seconds: int = Field(default=3600, gt=0)
    allowed_redirect_domains: list[str] = Field(default_factory=list)
    callback_base_url: str = ""
