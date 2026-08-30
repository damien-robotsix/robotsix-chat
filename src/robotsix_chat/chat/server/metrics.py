"""Prometheus metrics for mobile SSO authentication endpoints.

Exposes counters that track the lifecycle of mobile SSO token operations:
issuance, exchange, revocation, and validation failures.
"""

from __future__ import annotations

from prometheus_client import Counter

# ---------------------------------------------------------------------------
# Mobile SSO metrics
# ---------------------------------------------------------------------------

AUTH_LOGIN_REQUESTS = Counter(
    "robotsix_chat_auth_login_requests_total",
    "Total GET /auth/login requests (subject-token issuance initiations).",
    ["status"],
)

AUTH_CALLBACK_REQUESTS = Counter(
    "robotsix_chat_auth_callback_requests_total",
    "Total GET /auth/callback requests (tinyauth callback redirects).",
    ["status"],
)

MOBILE_TOKEN_EXCHANGE_REQUESTS = Counter(
    "robotsix_chat_mobile_token_exchange_requests_total",
    "Total POST /chat/auth/mobile-token requests (token exchange attempts).",
    ["status"],
)

REDIRECT_VALIDATION_REJECTIONS = Counter(
    "robotsix_chat_redirect_validation_rejections_total",
    "Total redirect-validation failures (domain not in allowlist).",
)

TOKEN_ISSUANCE_EVENTS = Counter(
    "robotsix_chat_token_issuance_events_total",
    "Total bearer tokens issued by /chat/auth/mobile-token.",
)

TOKEN_VERIFICATION_FAILURES = Counter(
    "robotsix_chat_token_verification_failures_total",
    "Total bearer token verification failures (expired, tampered, or invalid).",
    ["reason"],
)
