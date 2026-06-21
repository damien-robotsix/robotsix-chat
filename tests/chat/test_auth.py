"""Tests for HTTP Basic Auth gating the chat server."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest

from robotsix_chat.chat.auth import BasicAuthConfig
from robotsix_chat.chat.server import create_app
from tests.conftest import http_client


class _MockAgent:
    """Minimal :class:`ChatAgent` yielding a single token."""

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str]:
        yield "ok"


def _basic_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


AUTH = BasicAuthConfig(username="admin", password="s3cret")


@pytest.mark.asyncio
async def test_requires_credentials() -> None:
    """Without credentials, the UI and /chat return 401 + WWW-Authenticate."""
    app = create_app(_MockAgent(), auth=AUTH)

    async with http_client(app) as client:
        ui = await client.get("/")
        chat = await client.post("/chat", json={"message": "hi"})

    assert ui.status_code == 401
    assert ui.headers["www-authenticate"].startswith("Basic")
    assert chat.status_code == 401


@pytest.mark.asyncio
async def test_valid_credentials_pass() -> None:
    """Correct credentials are admitted to a protected route."""
    app = create_app(_MockAgent(), auth=AUTH)

    async with http_client(app) as client:
        response = await client.get("/", headers=_basic_header("admin", "s3cret"))

    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text


@pytest.mark.asyncio
async def test_wrong_password_rejected() -> None:
    """A valid username with the wrong password is rejected."""
    app = create_app(_MockAgent(), auth=AUTH)

    async with http_client(app) as client:
        response = await client.get("/", headers=_basic_header("admin", "nope"))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_malformed_authorization_header_rejected() -> None:
    """A non-Basic / un-decodable Authorization header is rejected, not crashed."""
    app = create_app(_MockAgent(), auth=AUTH)

    async with http_client(app) as client:
        bearer = await client.get("/", headers={"Authorization": "Bearer abc"})
        garbage = await client.get("/", headers={"Authorization": "Basic !!!notb64"})

    assert bearer.status_code == 401
    assert garbage.status_code == 401


@pytest.mark.asyncio
async def test_health_is_not_gated() -> None:
    """``GET /health`` stays open so liveness probes work without auth."""
    app = create_app(_MockAgent(), auth=AUTH)

    async with http_client(app) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_no_auth_leaves_server_open() -> None:
    """Without an *auth* config the server is reachable without credentials."""
    app = create_app(_MockAgent())

    async with http_client(app) as client:
        response = await client.get("/")

    assert response.status_code == 200
