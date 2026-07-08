"""Tests for the deploy-lifecycle API integration.

:func:`build_lifecycle_tools` and :class:`LifecycleClient`, with ``respx``
mocked so there are no real network calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import LifecycleSettings
from robotsix_chat.lifecycle import build_lifecycle_tools, load_lifecycle_skill
from robotsix_chat.lifecycle.client import LifecycleClient


def _settings(**kw: Any) -> LifecycleSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://lifecycle:9000",
        "api_key": "test-api-key",  # pragma: allowlist secret
    }
    base.update(kw)
    return LifecycleSettings(**base)


# ---------------------------------------------------------------------------
# build_lifecycle_tools
# ---------------------------------------------------------------------------


def test_build_lifecycle_tools_disabled() -> None:
    """Verify that disabled lifecycle returns no tools."""
    assert build_lifecycle_tools(LifecycleSettings(enabled=False)) == []


def test_build_lifecycle_tools_returns_four_read_only_tools() -> None:
    """Enabled lifecycle returns four read-only callables, no mutation tools."""
    tools = build_lifecycle_tools(_settings())
    names = {t.__name__ for t in tools}
    assert names == {
        "list_lifecycle_services",
        "get_lifecycle_service_status",
        "get_lifecycle_service_config",
        "get_lifecycle_service_env",
    }


# ---------------------------------------------------------------------------
# load_lifecycle_skill
# ---------------------------------------------------------------------------


def test_load_lifecycle_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and contains allowed/forbidden ops."""
    skill = load_lifecycle_skill()
    assert len(skill) > 100
    assert "read-only" in skill.lower()
    assert "Forbidden operations" in skill
    assert "list_lifecycle_services" in skill
    assert "POST /services/{name}/restart" in skill


# ---------------------------------------------------------------------------
# LifecycleClient — X-API-Key header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_sends_x_api_key_header(
    respx_mock: respx.MockRouter,
) -> None:
    """The lifecycle client sends X-API-Key when an api_key is configured."""
    route = respx_mock.get("http://lifecycle:9000/services").mock(
        return_value=httpx.Response(200, json={"services": []})
    )

    client = LifecycleClient(_settings(api_key="secret-key"))
    await client.list_services()

    assert route.calls.last.request.headers["x-api-key"] == "secret-key"


@pytest.mark.asyncio
async def test_client_no_x_api_key_when_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """The lifecycle client does NOT send X-API-Key when api_key is empty."""
    route = respx_mock.get("http://lifecycle:9000/services").mock(
        return_value=httpx.Response(200, json={"services": []})
    )

    client = LifecycleClient(_settings(api_key=""))
    await client.list_services()

    assert "x-api-key" not in route.calls.last.request.headers


# ---------------------------------------------------------------------------
# LifecycleClient — tool output (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_services_returns_json(
    respx_mock: respx.MockRouter,
) -> None:
    """list_lifecycle_services returns formatted JSON on success."""
    respx_mock.get("http://lifecycle:9000/services").mock(
        return_value=httpx.Response(
            200,
            json={
                "services": [
                    {"name": "robotsix-chat", "status": "running"},
                    {"name": "robotsix-mill", "status": "running"},
                ]
            },
        )
    )

    client = LifecycleClient(_settings())
    out = await client.list_services()
    assert "robotsix-chat" in out
    assert "robotsix-mill" in out
    assert "running" in out


@pytest.mark.asyncio
async def test_service_status_returns_json(
    respx_mock: respx.MockRouter,
) -> None:
    """get_lifecycle_service_status returns formatted status JSON."""
    respx_mock.get("http://lifecycle:9000/services/chat/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "chat",
                "status": "running",
                "health_checks": [{"type": "http", "ok": True}],
            },
        )
    )

    client = LifecycleClient(_settings())
    out = await client.service_status("chat")
    assert "running" in out
    assert "health_checks" in out


@pytest.mark.asyncio
async def test_service_config_returns_masked_secrets(
    respx_mock: respx.MockRouter,
) -> None:
    """get_lifecycle_service_config returns config with secrets masked."""
    respx_mock.get("http://lifecycle:9000/services/chat/config").mock(
        return_value=httpx.Response(
            200,
            json={
                "server": {"port": 8080},
                "api_key": "***",
                "database_url": "***",
            },
        )
    )

    client = LifecycleClient(_settings())
    out = await client.service_config("chat")
    assert "***" in out
    assert "8080" in out


@pytest.mark.asyncio
async def test_service_env_returns_masked_secrets(
    respx_mock: respx.MockRouter,
) -> None:
    """get_lifecycle_service_env returns environment with secrets masked."""
    respx_mock.get("http://lifecycle:9000/services/chat/env").mock(
        return_value=httpx.Response(
            200,
            json={
                "LOG_LEVEL": "INFO",
                "DATABASE_URL": "***",
            },
        )
    )

    client = LifecycleClient(_settings())
    out = await client.service_env("chat")
    assert "***" in out
    assert "LOG_LEVEL" in out


# ---------------------------------------------------------------------------
# LifecycleClient — error handling (no raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_services_http_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """An HTTP error is returned as a concise string, never raised."""
    respx_mock.get("http://lifecycle:9000/services").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )

    client = LifecycleClient(_settings())
    out = await client.list_services()
    assert "Lifecycle" in out
    assert "500" in out


@pytest.mark.asyncio
async def test_service_status_network_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A network/connection error is returned as a string, never raised."""
    respx_mock.get("http://lifecycle:9000/services/chat/status").mock(
        side_effect=ConnectionError("connection refused")
    )

    client = LifecycleClient(_settings())
    out = await client.service_status("chat")
    assert "Lifecycle" in out
    assert "connection refused" in out.lower()


@pytest.mark.asyncio
async def test_non_json_response_returns_raw_text(
    respx_mock: respx.MockRouter,
) -> None:
    """A non-JSON response is returned as plain text."""
    respx_mock.get("http://lifecycle:9000/services").mock(
        return_value=httpx.Response(200, text="plain text response")
    )

    client = LifecycleClient(_settings())
    out = await client.list_services()
    assert "plain text response" in out
