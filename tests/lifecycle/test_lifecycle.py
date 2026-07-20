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


def test_build_lifecycle_tools_returns_nine_tools_including_mutations() -> None:
    """Enabled lifecycle returns nine tools including mutation tools."""
    tools = build_lifecycle_tools(_settings())
    names = {t.__name__ for t in tools}
    assert names == {
        "list_lifecycle_services",
        "get_lifecycle_service_status",
        "get_lifecycle_service_config",
        "get_lifecycle_service_env",
        "watch_service_redeploy",
        "restart_lifecycle_service",
        "update_lifecycle_service_config",
        "update_lifecycle_service_env",
        "self_restart",
    }


# ---------------------------------------------------------------------------
# load_lifecycle_skill
# ---------------------------------------------------------------------------


def test_load_lifecycle_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and contains allowed/restricted ops."""
    skill = load_lifecycle_skill()
    assert len(skill) > 100
    assert "inspection and mutation" in skill.lower()
    assert "Restricted operations" in skill
    assert "list_lifecycle_services" in skill
    assert "restart_lifecycle_service" in skill
    assert "self_restart" in skill
    assert "Self-restart" in skill


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


# ---------------------------------------------------------------------------
# LifecycleClient — mutation methods (restart, config-write, env-write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_service_success(
    respx_mock: respx.MockRouter,
) -> None:
    """restart_service sends POST and returns formatted response."""
    route = respx_mock.post("http://lifecycle:9000/services/chat/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )

    client = LifecycleClient(_settings())
    out = await client.restart_service("chat")
    assert '"status": "restarting"' in out
    assert route.calls.last.request.headers["x-api-key"] == "test-api-key"


@pytest.mark.asyncio
async def test_restart_service_403_returns_error_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A 403 (toggle disabled) is returned as an error string, not raised."""
    respx_mock.post("http://lifecycle:9000/services/chat/restart").mock(
        return_value=httpx.Response(
            403,
            json={"error": 'Chat agent is not permitted to mutate service "chat".'},
        )
    )

    client = LifecycleClient(_settings())
    out = await client.restart_service("chat")
    assert "Lifecycle" in out
    assert "403" in out


# ---------------------------------------------------------------------------
# LifecycleClient — self_restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_restart_success(
    respx_mock: respx.MockRouter,
) -> None:
    """self_restart sends POST /self/restart and returns formatted response."""
    route = respx_mock.post("http://lifecycle:9000/self/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )

    client = LifecycleClient(_settings())
    out = await client.self_restart()
    assert '"status": "restarting"' in out
    assert route.calls.last.request.headers["x-api-key"] == "test-api-key"


@pytest.mark.asyncio
async def test_self_restart_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A server error on self_restart is returned as a string, not raised."""
    respx_mock.post("http://lifecycle:9000/self/restart").mock(
        return_value=httpx.Response(
            500,
            json={"error": "internal server error"},
        )
    )

    client = LifecycleClient(_settings())
    out = await client.self_restart()
    assert "Lifecycle" in out
    assert "500" in out


@pytest.mark.asyncio
async def test_self_restart_tool_is_registered() -> None:
    """The self_restart tool is returned by build_lifecycle_tools."""
    tools = build_lifecycle_tools(_settings())
    names = {t.__name__ for t in tools}
    assert "self_restart" in names


@pytest.mark.asyncio
async def test_self_restart_tool_calls_client_self_restart(
    respx_mock: respx.MockRouter,
) -> None:
    """Calling the self_restart tool invokes the client's self_restart method."""
    route = respx_mock.post("http://lifecycle:9000/self/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )

    tools = build_lifecycle_tools(_settings())
    self_restart_tool = next(t for t in tools if t.__name__ == "self_restart")
    out = await self_restart_tool()
    assert '"status": "restarting"' in out
    assert route.calls.last.request.headers["x-api-key"] == "test-api-key"


@pytest.mark.asyncio
async def test_update_service_config_success(
    respx_mock: respx.MockRouter,
) -> None:
    """update_service_config sends PUT with JSON body and returns response."""
    route = respx_mock.put("http://lifecycle:9000/services/chat/config").mock(
        return_value=httpx.Response(200, json={"updated": ["log_level"]})
    )

    client = LifecycleClient(_settings())
    out = await client.update_service_config("chat", {"log_level": "DEBUG"})
    assert "log_level" in out
    assert "updated" in out
    assert route.calls.last.request.headers["x-api-key"] == "test-api-key"


@pytest.mark.asyncio
async def test_update_service_env_success(
    respx_mock: respx.MockRouter,
) -> None:
    """update_service_env sends PUT with JSON body and returns response."""
    route = respx_mock.put("http://lifecycle:9000/services/chat/env").mock(
        return_value=httpx.Response(200, json={"updated": ["MY_VAR"]})
    )

    client = LifecycleClient(_settings())
    out = await client.update_service_env("chat", {"MY_VAR": "new_value"})
    assert "MY_VAR" in out
    assert "updated" in out
    assert route.calls.last.request.headers["x-api-key"] == "test-api-key"


@pytest.mark.asyncio
async def test_update_service_config_403_returns_error_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A 403 on config-write is returned as an error string."""
    respx_mock.put("http://lifecycle:9000/services/chat/config").mock(
        return_value=httpx.Response(
            403,
            json={"error": 'Chat agent is not permitted to mutate service "chat".'},
        )
    )

    client = LifecycleClient(_settings())
    out = await client.update_service_config("chat", {"log_level": "DEBUG"})
    assert "Lifecycle" in out
    assert "403" in out


# ---------------------------------------------------------------------------
# watch_service_redeploy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_service_redeploy_detects_config_change(
    respx_mock: respx.MockRouter,
) -> None:
    """Config change is detected and returned as a success summary."""
    call_count = 0

    def config_response(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"image": "digest-aaa"})
        if call_count == 2:
            return httpx.Response(200, json={"image": "digest-aaa"})
        # Third call — config changed (redeploy rolled out).
        return httpx.Response(200, json={"image": "digest-bbb"})

    def status_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "running"})

    respx_mock.get("http://lifecycle:9000/services/mill/config").mock(
        side_effect=config_response
    )
    respx_mock.get("http://lifecycle:9000/services/mill/status").mock(
        side_effect=status_response
    )

    client = LifecycleClient(_settings())
    out = await client.watch_service_redeploy(
        "mill", max_wait_seconds=30.0, poll_interval_seconds=5.0
    )
    assert "Redeploy detected" in out
    assert "mill" in out
    assert '"status": "running"' in out


@pytest.mark.asyncio
async def test_watch_service_redeploy_times_out(
    respx_mock: respx.MockRouter,
) -> None:
    """When config never changes, the tool times out with a helpful message."""
    respx_mock.get("http://lifecycle:9000/services/mill/config").mock(
        return_value=httpx.Response(200, json={"image": "digest-aaa"})
    )
    respx_mock.get("http://lifecycle:9000/services/mill/status").mock(
        return_value=httpx.Response(200, json={"status": "running"})
    )

    client = LifecycleClient(_settings())
    out = await client.watch_service_redeploy(
        "mill", max_wait_seconds=0.1, poll_interval_seconds=5.0
    )
    assert "Timeout" in out
    assert "mill" in out
    assert "manual redeploy" in out.lower()


@pytest.mark.asyncio
async def test_watch_service_redeploy_initial_failure_returns_error(
    respx_mock: respx.MockRouter,
) -> None:
    """When the initial config fetch fails, an error message is returned."""
    respx_mock.get("http://lifecycle:9000/services/mill/config").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )
    respx_mock.get("http://lifecycle:9000/services/mill/status").mock(
        return_value=httpx.Response(200, json={"status": "running"})
    )

    client = LifecycleClient(_settings())
    out = await client.watch_service_redeploy(
        "mill", max_wait_seconds=30.0, poll_interval_seconds=5.0
    )
    assert "Could not reach" in out
    assert "mill" in out


@pytest.mark.asyncio
async def test_watch_service_redeploy_recovers_from_intermittent_failure(
    respx_mock: respx.MockRouter,
) -> None:
    """A transient poll failure is logged and retried — the tool continues."""
    call_count = 0

    def config_response(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"image": "digest-aaa"})
        if call_count == 2:
            # Transient failure — should be retried.
            return httpx.Response(503)
        # Third call — redeploy detected.
        return httpx.Response(200, json={"image": "digest-bbb"})

    respx_mock.get("http://lifecycle:9000/services/mill/config").mock(
        side_effect=config_response
    )
    respx_mock.get("http://lifecycle:9000/services/mill/status").mock(
        return_value=httpx.Response(200, json={"status": "running"})
    )

    client = LifecycleClient(_settings())
    out = await client.watch_service_redeploy(
        "mill", max_wait_seconds=30.0, poll_interval_seconds=5.0
    )
    assert "Redeploy detected" in out


@pytest.mark.asyncio
async def test_watch_service_redeploy_clamps_poll_interval(
    respx_mock: respx.MockRouter,
) -> None:
    """poll_interval_seconds below the minimum is clamped to 5 s."""
    call_count = 0

    def config_response(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"image": "digest-aaa"})
        return httpx.Response(200, json={"image": "digest-bbb"})

    respx_mock.get("http://lifecycle:9000/services/mill/config").mock(
        side_effect=config_response
    )
    respx_mock.get("http://lifecycle:9000/services/mill/status").mock(
        return_value=httpx.Response(200, json={"status": "running"})
    )

    client = LifecycleClient(_settings())
    # A sub-minimum interval should not break anything — the tool clamps
    # it internally and still detects the redeploy.
    out = await client.watch_service_redeploy(
        "mill", max_wait_seconds=30.0, poll_interval_seconds=0.1
    )
    assert "Redeploy detected" in out


@pytest.mark.asyncio
async def test_watch_service_redeploy_non_json_status_is_raw_text(
    respx_mock: respx.MockRouter,
) -> None:
    """A non-JSON status response on redeploy detection is returned as-is."""
    call_count = 0

    def config_response(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"image": "digest-aaa"})
        return httpx.Response(200, json={"image": "digest-bbb"})

    respx_mock.get("http://lifecycle:9000/services/mill/config").mock(
        side_effect=config_response
    )
    respx_mock.get("http://lifecycle:9000/services/mill/status").mock(
        return_value=httpx.Response(200, text="status: healthy (plain text)")
    )

    client = LifecycleClient(_settings())
    out = await client.watch_service_redeploy(
        "mill", max_wait_seconds=30.0, poll_interval_seconds=5.0
    )
    assert "Redeploy detected" in out
    assert "status: healthy (plain text)" in out
