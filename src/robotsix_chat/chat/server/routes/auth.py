"""Mobile SSO authentication endpoints.

``GET /auth/login`` — initiates the tinyauth redirect handshake and
redirects back to the mobile app via a validated deep-link allowlist.

``POST /chat/auth/mobile-token`` — exchanges the tinyauth edge-header
identity for a short-lived HMAC-signed bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode, urlparse

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from robotsix_chat.chat.server.metrics import (
    AUTH_CALLBACK_REQUESTS,
    AUTH_LOGIN_REQUESTS,
    MOBILE_TOKEN_EXCHANGE_REQUESTS,
    REDIRECT_VALIDATION_REJECTIONS,
    TOKEN_ISSUANCE_EVENTS,
    TOKEN_VERIFICATION_FAILURES,
)
from robotsix_chat.config.models import MobileAuthSettings

logger = logging.getLogger(__name__)


def _get_mobile_auth(request: Request) -> MobileAuthSettings:
    """Return the ``MobileAuthSettings`` from app state, or 404 if disabled."""
    settings: MobileAuthSettings | None = getattr(
        request.app.state, "mobile_auth", None
    )
    if settings is None or not settings.enabled:
        raise HTTPException(status_code=404, detail="mobile auth is not enabled")
    return settings


def _domain_allowed(url: str, allowed_domains: list[str]) -> bool:
    """Return ``True`` if *url*'s hostname is in *allowed_domains*."""
    hostname = urlparse(url).hostname
    if hostname is None:
        return False
    return hostname in allowed_domains


def _sign_token(subject: str, secret: str, ttl: int) -> str:
    """Create an HMAC-SHA256 signed bearer token for *subject*.

    The token encodes ``subject|expiry`` and an HMAC tag so the server
    can verify it without storing state.
    """
    expiry = int(time.time()) + ttl
    payload = f"{subject}|{expiry}"
    tag = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{tag}"


def _verify_token(token: str, secret: str) -> str | None:
    """Verify a bearer token and return the subject, or ``None`` on failure."""
    parts = token.split("|")
    if len(parts) != 3:
        return None
    subject, expiry_str, tag = parts
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if time.time() > expiry:
        return None
    expected_payload = f"{subject}|{expiry_str}"
    expected_tag = hmac.new(
        secret.encode(), expected_payload.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(tag, expected_tag):
        return None
    return subject


async def auth_login_endpoint(request: Request) -> RedirectResponse:
    """Initiate the tinyauth redirect handshake for mobile SSO.

    Query parameters:
        redirect_to: Deep-link URL the mobile app wants to be redirected
            back to after authentication.  Must match an allowed domain.

    Redirects the user's browser to the tinyauth login page with a
    ``callback`` parameter pointing back to this server.  Tinyauth will
    authenticate the user and redirect back to ``callback``, which
    includes the original ``redirect_to`` so the server can forward the
    user to the app.
    """
    auth = _get_mobile_auth(request)

    redirect_to = request.query_params.get("redirect_to")
    if not redirect_to:
        AUTH_LOGIN_REQUESTS.labels(status="400").inc()
        raise HTTPException(
            status_code=400,
            detail="redirect_to query parameter is required",
        )

    if not _domain_allowed(redirect_to, auth.allowed_redirect_domains):
        REDIRECT_VALIDATION_REJECTIONS.inc()
        AUTH_LOGIN_REQUESTS.labels(status="400").inc()
        logger.warning(
            "auth_login: redirect validation rejected",
            extra={
                "redirect_to": redirect_to,
                "allowed_domains": auth.allowed_redirect_domains,
                "event": "redirect_validation_rejection",
            },
        )
        raise HTTPException(
            status_code=400,
            detail="redirect_to domain is not in the allowlist",
        )

    # Build the callback URL that tinyauth will redirect back to.
    # It includes the original redirect_to so the callback handler
    # (or tinyauth itself) can forward the user to the mobile app.
    callback_url = (
        f"{auth.callback_base_url.rstrip('/')}/auth/callback"
        f"?{urlencode({'redirect_to': redirect_to})}"
    )

    # Redirect to tinyauth's login page with the callback.
    tinyauth_login = (
        f"{auth.tinyauth_url.rstrip('/')}/login?{urlencode({'callback': callback_url})}"
    )

    AUTH_LOGIN_REQUESTS.labels(status="302").inc()
    logger.info(
        "auth_login: redirecting to tinyauth",
        extra={
            "redirect_to": redirect_to,
            "tinyauth_url": tinyauth_login,
            "event": "auth_login_redirect",
        },
    )
    return RedirectResponse(url=tinyauth_login, status_code=302)


async def auth_callback_endpoint(request: Request) -> RedirectResponse:
    """Handle the tinyauth callback after successful authentication.

    Tinyauth redirects here after the user authenticates.  The edge
    proxy (tinyauth) sets the identity headers on the proxied request.
    This endpoint reads the ``redirect_to`` from the query string and
    redirects the user's browser to the mobile app's deep-link.
    """
    auth = _get_mobile_auth(request)

    redirect_to = request.query_params.get("redirect_to")
    if not redirect_to:
        AUTH_CALLBACK_REQUESTS.labels(status="400").inc()
        raise HTTPException(
            status_code=400,
            detail="redirect_to query parameter is required",
        )

    if not _domain_allowed(redirect_to, auth.allowed_redirect_domains):
        REDIRECT_VALIDATION_REJECTIONS.inc()
        AUTH_CALLBACK_REQUESTS.labels(status="400").inc()
        logger.warning(
            "auth_callback: redirect validation rejected",
            extra={
                "redirect_to": redirect_to,
                "allowed_domains": auth.allowed_redirect_domains,
                "event": "redirect_validation_rejection",
            },
        )
        raise HTTPException(
            status_code=400,
            detail="redirect_to domain is not in the allowlist",
        )

    # The identity is set by the tinyauth edge proxy in the header.
    subject = request.headers.get(auth.subject_header)
    if not subject:
        AUTH_CALLBACK_REQUESTS.labels(status="401").inc()
        raise HTTPException(
            status_code=401,
            detail="missing identity header — request must pass through tinyauth",
        )

    AUTH_CALLBACK_REQUESTS.labels(status="302").inc()
    logger.info(
        "auth_callback: redirecting to app",
        extra={
            "subject": subject,
            "redirect_to": redirect_to,
            "event": "auth_callback_redirect",
        },
    )
    return RedirectResponse(url=redirect_to, status_code=302)


async def mobile_token_endpoint(request: Request) -> JSONResponse:
    """Exchange the tinyauth edge-header identity for a short-lived bearer token.

    The identity is **always** taken from the tinyauth edge header
    (``subject_header``) — never from the request body.  This prevents
    subject spoofing: only the trusted reverse proxy can set the header.

    Returns a JSON object with ``token``, ``subject``, and ``expires_in``.
    """
    auth = _get_mobile_auth(request)

    subject = request.headers.get(auth.subject_header)
    if not subject:
        MOBILE_TOKEN_EXCHANGE_REQUESTS.labels(status="401").inc()
        TOKEN_VERIFICATION_FAILURES.labels(reason="missing_header").inc()
        logger.warning(
            "mobile_token: missing identity header",
            extra={"event": "token_exchange_failure", "reason": "missing_header"},
        )
        raise HTTPException(
            status_code=401,
            detail="missing identity header — request must pass through tinyauth",
        )

    secret = auth.token_secret.get_secret_value()
    if not secret:
        MOBILE_TOKEN_EXCHANGE_REQUESTS.labels(status="500").inc()
        raise HTTPException(
            status_code=500,
            detail="token_secret is not configured",
        )

    token = _sign_token(subject, secret, auth.token_ttl_seconds)

    TOKEN_ISSUANCE_EVENTS.inc()
    MOBILE_TOKEN_EXCHANGE_REQUESTS.labels(status="200").inc()
    logger.info(
        "mobile_token: issued token",
        extra={
            "subject": subject,
            "ttl": auth.token_ttl_seconds,
            "event": "token_issuance",
        },
    )
    return JSONResponse(
        {
            "token": token,
            "subject": subject,
            "expires_in": auth.token_ttl_seconds,
        }
    )
