"""Tests for mobile SSO authentication endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.testclient import TestClient

from robotsix_chat.chat.server.routes.auth import (
    _domain_allowed,
    _sign_token,
    _verify_token,
    auth_callback_endpoint,
    auth_login_endpoint,
    mobile_token_endpoint,
)
from robotsix_chat.chat.server.routes.errors import (
    http_exception_handler,
    not_found_handler,
    unhandled_exception_handler,
)
from robotsix_chat.config.models import MobileAuthSettings


def _make_app(auth: MobileAuthSettings) -> Starlette:
    """Build a minimal Starlette app with the auth endpoints wired."""
    app = Starlette(
        routes=[
            Route("/auth/login", auth_login_endpoint, methods=["GET"]),
            Route("/auth/callback", auth_callback_endpoint, methods=["GET"]),
            Route(
                "/chat/auth/mobile-token",
                mobile_token_endpoint,
                methods=["POST"],
            ),
        ],
        exception_handlers={
            HTTPException: http_exception_handler,
            404: not_found_handler,
            Exception: unhandled_exception_handler,
        },
    )
    app.state.mobile_auth = auth
    return app


def _enabled_auth(**overrides: Any) -> MobileAuthSettings:
    """Return a ``MobileAuthSettings`` with sane defaults for testing."""
    defaults = dict(
        enabled=True,
        tinyauth_url="https://auth.example.com",
        subject_header="X-Forwarded-User",
        session_header="X-Forwarded-Session",
        token_secret=SecretStr("test-secret-key"),
        token_ttl_seconds=3600,
        allowed_redirect_domains=["app.example.com"],
        callback_base_url="https://chat.example.com",
    )
    defaults.update(overrides)
    return MobileAuthSettings(**defaults)


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestDomainAllowed:
    """Tests for ``_domain_allowed``."""

    def test_allowed_domain(self) -> None:
        """Allowed domain returns True."""
        assert (
            _domain_allowed("https://app.example.com/path", ["app.example.com"]) is True
        )

    def test_disallowed_domain(self) -> None:
        """Disallowed domain returns False."""
        assert _domain_allowed("https://evil.com/path", ["app.example.com"]) is False

    def test_no_hostname(self) -> None:
        """URL without hostname returns False."""
        assert _domain_allowed("not-a-url", ["app.example.com"]) is False

    def test_subdomain_not_allowed(self) -> None:
        """Subdomain is not implicitly allowed."""
        assert (
            _domain_allowed("https://sub.app.example.com", ["app.example.com"]) is False
        )


class TestTokenSigning:
    """Tests for ``_sign_token`` and ``_verify_token``."""

    def test_roundtrip(self) -> None:
        """Signed token verifies correctly."""
        token = _sign_token("alice", "secret", 3600)
        assert _verify_token(token, "secret") == "alice"

    def test_wrong_secret(self) -> None:
        """Token signed with different secret fails verification."""
        token = _sign_token("alice", "secret", 3600)
        assert _verify_token(token, "wrong-secret") is None

    def test_expired_token(self) -> None:
        """Expired token fails verification."""
        token = _sign_token("alice", "secret", -1)
        assert _verify_token(token, "secret") is None

    def test_malformed_token(self) -> None:
        """Malformed token string fails verification."""
        assert _verify_token("not-a-token", "secret") is None

    def test_tampered_payload(self) -> None:
        """Tampered payload fails verification."""
        token = _sign_token("alice", "secret", 3600)
        parts = token.split("|")
        parts[0] = "bob"
        tampered = "|".join(parts)
        assert _verify_token(tampered, "secret") is None


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------


class TestAuthLogin:
    """Tests for ``GET /auth/login``."""

    def test_redirects_to_tinyauth(self) -> None:
        """Valid redirect_to triggers tinyauth redirect."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/login",
            params={"redirect_to": "https://app.example.com/callback"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "auth.example.com/login" in location
        assert "redirect_uri=" in location
        assert "login_for=app" in location

    def test_missing_redirect_to_returns_400(self) -> None:
        """Missing redirect_to parameter returns 400."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.get("/auth/login")
        assert resp.status_code == 400
        assert "redirect_to" in resp.json()["error"]

    def test_disallowed_domain_returns_400(self) -> None:
        """Domain not in allowlist returns 400."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/login",
            params={"redirect_to": "https://evil.com/steal"},
        )
        assert resp.status_code == 400
        assert "allowlist" in resp.json()["error"]

    def test_disabled_returns_404(self) -> None:
        """Disabled mobile auth returns 404."""
        auth = MobileAuthSettings(enabled=False)
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/login",
            params={"redirect_to": "https://app.example.com/callback"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /auth/callback
# ---------------------------------------------------------------------------


class TestAuthCallback:
    """Tests for ``GET /auth/callback``."""

    def test_redirects_to_app(self) -> None:
        """Valid callback redirects to the app with a signed token."""
        auth = _enabled_auth()
        app = _make_app(auth)
        client = TestClient(app)
        resp = client.get(
            "/auth/callback",
            params={"redirect_to": "https://app.example.com/done"},
            headers={"X-Forwarded-User": "alice"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://app.example.com/done")
        # The redirect URL must carry a token query parameter.
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(location).query)
        assert "token" in qs
        token = qs["token"][0]
        assert _verify_token(token, "test-secret-key") == "alice"

    def test_redirect_preserves_existing_query(self) -> None:
        """Token is appended without clobbering existing query params."""
        auth = _enabled_auth()
        app = _make_app(auth)
        client = TestClient(app)
        resp = client.get(
            "/auth/callback",
            params={"redirect_to": "https://app.example.com/done?foo=bar"},
            headers={"X-Forwarded-User": "alice"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(location).query)
        assert qs["foo"] == ["bar"]
        assert "token" in qs
        assert _verify_token(qs["token"][0], "test-secret-key") == "alice"

    def test_empty_secret_returns_500(self) -> None:
        """Empty token_secret returns 500 from callback."""
        auth = _enabled_auth(token_secret=SecretStr(""))
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/callback",
            params={"redirect_to": "https://app.example.com/done"},
            headers={"X-Forwarded-User": "alice"},
        )
        assert resp.status_code == 500

    def test_missing_header_returns_401(self) -> None:
        """Missing identity header returns 401."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/callback",
            params={"redirect_to": "https://app.example.com/done"},
        )
        assert resp.status_code == 401
        assert "identity header" in resp.json()["error"]

    def test_missing_redirect_to_returns_400(self) -> None:
        """Missing redirect_to returns 400."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/callback",
            headers={"X-Forwarded-User": "alice"},
        )
        assert resp.status_code == 400

    def test_disallowed_domain_returns_400(self) -> None:
        """Domain not in allowlist returns 400."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/callback",
            params={"redirect_to": "https://evil.com/steal"},
            headers={"X-Forwarded-User": "alice"},
        )
        assert resp.status_code == 400

    def test_disabled_returns_404(self) -> None:
        """Disabled mobile auth returns 404."""
        auth = MobileAuthSettings(enabled=False)
        client = TestClient(_make_app(auth))
        resp = client.get(
            "/auth/callback",
            params={"redirect_to": "https://app.example.com/done"},
            headers={"X-Forwarded-User": "alice"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /chat/auth/mobile-token
# ---------------------------------------------------------------------------


class TestMobileToken:
    """Tests for ``POST /chat/auth/mobile-token``."""

    def test_issued_token(self) -> None:
        """Valid identity header issues a signed token."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.post(
            "/chat/auth/mobile-token",
            headers={"X-Forwarded-User": "alice"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["subject"] == "alice"
        assert body["expires_in"] == 3600
        assert "token" in body
        # Verify the token is valid
        assert _verify_token(body["token"], "test-secret-key") == "alice"

    def test_missing_header_returns_401(self) -> None:
        """Missing identity header returns 401."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.post("/chat/auth/mobile-token")
        assert resp.status_code == 401
        assert "identity header" in resp.json()["error"]

    def test_disabled_returns_404(self) -> None:
        """Disabled mobile auth returns 404."""
        auth = MobileAuthSettings(enabled=False)
        client = TestClient(_make_app(auth))
        resp = client.post(
            "/chat/auth/mobile-token",
            headers={"X-Forwarded-User": "alice"},
        )
        assert resp.status_code == 404

    def test_custom_subject_header(self) -> None:
        """Custom subject_header is respected."""
        auth = _enabled_auth(subject_header="X-Custom-User")
        client = TestClient(_make_app(auth))
        resp = client.post(
            "/chat/auth/mobile-token",
            headers={"X-Custom-User": "bob"},
        )
        assert resp.status_code == 200
        assert resp.json()["subject"] == "bob"

    def test_empty_secret_returns_500(self) -> None:
        """Empty token_secret returns 500."""
        auth = _enabled_auth(token_secret=SecretStr(""))
        client = TestClient(_make_app(auth))
        resp = client.post(
            "/chat/auth/mobile-token",
            headers={"X-Forwarded-User": "alice"},
        )
        assert resp.status_code == 500

    def test_body_token_fallback(self) -> None:
        """Valid signed token in body (no header) returns 200."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        # Pre-sign a token the way the callback endpoint would.
        signed = _sign_token("alice", "test-secret-key", 3600)
        resp = client.post(
            "/chat/auth/mobile-token",
            json={"token": signed},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["subject"] == "alice"
        assert _verify_token(body["token"], "test-secret-key") == "alice"

    def test_body_token_invalid_returns_401(self) -> None:
        """Invalid signed token in body + no header returns 401."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.post(
            "/chat/auth/mobile-token",
            json={"token": "bad|token|value"},
        )
        assert resp.status_code == 401

    def test_body_token_missing_returns_401(self) -> None:
        """Empty body + no header returns 401."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        resp = client.post(
            "/chat/auth/mobile-token",
            json={},
        )
        assert resp.status_code == 401

    def test_header_takes_precedence_over_body(self) -> None:
        """Edge header identity takes precedence over body token."""
        auth = _enabled_auth()
        client = TestClient(_make_app(auth))
        signed = _sign_token("bob", "test-secret-key", 3600)
        resp = client.post(
            "/chat/auth/mobile-token",
            headers={"X-Forwarded-User": "alice"},
            json={"token": signed},
        )
        assert resp.status_code == 200
        assert resp.json()["subject"] == "alice"


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------


class TestAuthMetrics:
    """Tests for Prometheus metrics emitted by auth endpoints."""

    def test_auth_login_increments_counter(self) -> None:
        """GET /auth/login increments the auth_login_requests counter."""
        from robotsix_chat.chat.server.metrics import AUTH_LOGIN_REQUESTS

        auth = _enabled_auth()
        client = TestClient(_make_app(auth))

        # Get initial value
        initial = AUTH_LOGIN_REQUESTS.labels(status="302")._value.get()

        client.get(
            "/auth/login",
            params={"redirect_to": "https://app.example.com/callback"},
            follow_redirects=False,
        )

        assert AUTH_LOGIN_REQUESTS.labels(status="302")._value.get() == initial + 1

    def test_auth_login_rejection_increments_counter(self) -> None:
        """GET /auth/login with bad domain increments rejection counter."""
        from robotsix_chat.chat.server.metrics import (
            AUTH_LOGIN_REQUESTS,
            REDIRECT_VALIDATION_REJECTIONS,
        )

        auth = _enabled_auth()
        client = TestClient(_make_app(auth))

        initial_rejections = REDIRECT_VALIDATION_REJECTIONS._value.get()
        initial_400 = AUTH_LOGIN_REQUESTS.labels(status="400")._value.get()

        client.get(
            "/auth/login",
            params={"redirect_to": "https://evil.com/steal"},
        )

        assert REDIRECT_VALIDATION_REJECTIONS._value.get() == initial_rejections + 1
        assert AUTH_LOGIN_REQUESTS.labels(status="400")._value.get() == initial_400 + 1

    def test_mobile_token_increments_counter(self) -> None:
        """POST /chat/auth/mobile-token increments token exchange counter."""
        from robotsix_chat.chat.server.metrics import (
            MOBILE_TOKEN_EXCHANGE_REQUESTS,
            TOKEN_ISSUANCE_EVENTS,
        )

        auth = _enabled_auth()
        client = TestClient(_make_app(auth))

        initial_200 = MOBILE_TOKEN_EXCHANGE_REQUESTS.labels(status="200")._value.get()
        initial_issuance = TOKEN_ISSUANCE_EVENTS._value.get()

        client.post(
            "/chat/auth/mobile-token",
            headers={"X-Forwarded-User": "alice"},
        )

        assert (
            MOBILE_TOKEN_EXCHANGE_REQUESTS.labels(status="200")._value.get()
            == initial_200 + 1
        )
        assert TOKEN_ISSUANCE_EVENTS._value.get() == initial_issuance + 1

    def test_mobile_token_401_increments_counter(self) -> None:
        """POST /chat/auth/mobile-token without header increments 401 counter."""
        from robotsix_chat.chat.server.metrics import (
            MOBILE_TOKEN_EXCHANGE_REQUESTS,
            TOKEN_VERIFICATION_FAILURES,
        )

        auth = _enabled_auth()
        client = TestClient(_make_app(auth))

        initial_401 = MOBILE_TOKEN_EXCHANGE_REQUESTS.labels(status="401")._value.get()
        initial_failures = TOKEN_VERIFICATION_FAILURES.labels(
            reason="missing_header"
        )._value.get()

        resp = client.post("/chat/auth/mobile-token")
        assert resp.status_code == 401

        assert (
            MOBILE_TOKEN_EXCHANGE_REQUESTS.labels(status="401")._value.get()
            == initial_401 + 1
        )
        assert (
            TOKEN_VERIFICATION_FAILURES.labels(reason="missing_header")._value.get()
            == initial_failures + 1
        )
