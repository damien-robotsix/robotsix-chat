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
from robotsix_chat.lifecycle.client import (
    LifecycleClient,
    _diagnose_self_restart_failure,
    _ensure_url_scheme,
    _is_transient_error,
    _validate_http_url,
)


def _settings(**kw: Any) -> LifecycleSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://lifecycle:9000",
        "api_key": "test-api-key",  # pragma: allowlist secret
        "service_name": "chat",
    }
    base.update(kw)
    return LifecycleSettings(**base)


# ---------------------------------------------------------------------------
# build_lifecycle_tools
# ---------------------------------------------------------------------------


def test_build_lifecycle_tools_disabled() -> None:
    """Verify that disabled lifecycle returns no tools."""
    assert build_lifecycle_tools(LifecycleSettings(enabled=False)) == []


def test_build_lifecycle_tools_returns_six_tools_including_mutations() -> None:
    """Enabled lifecycle returns six tools including mutation tools."""
    tools = build_lifecycle_tools(_settings())
    names = {t.__name__ for t in tools}
    assert names == {
        "list_lifecycle_services",
        "get_lifecycle_service_status",
        "get_lifecycle_service_env",
        "restart_lifecycle_service",
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
    """self_restart POSTs /chat/services/{name}/restart and returns the body."""
    route = respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )

    client = LifecycleClient(_settings())
    out = await client.self_restart()
    assert '"status": "restarting"' in out
    assert "## self_restart failure diagnostic" not in out
    assert route.calls.last.request.headers["x-api-key"] == "test-api-key"


@pytest.mark.asyncio
async def test_self_restart_error_returns_string(
    respx_mock: respx.MockRouter,
) -> None:
    """A server error on self_restart is returned as a diagnostic report, not raised."""
    respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        return_value=httpx.Response(
            500,
            json={"error": "internal server error"},
        )
    )

    client = LifecycleClient(_settings())
    out = await client.self_restart()
    # 500 is transient — with 3 retries it exhausts and returns a diagnostic.
    assert "## self_restart failure diagnostic" in out
    assert "### What happened" in out
    assert "### Next steps" in out
    assert "### Raw error" in out
    assert "Lifecycle" in out
    assert "500" in out


@pytest.mark.asyncio
async def test_self_restart_unconfigured_service_name_returns_message() -> None:
    """With no service_name, self_restart returns a clear message (no call)."""
    client = LifecycleClient(_settings(service_name=""))
    out = await client.self_restart()
    assert "service_name" in out
    assert "not" in out.lower()


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
    route = respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )

    tools = build_lifecycle_tools(_settings())
    self_restart_tool = next(t for t in tools if t.__name__ == "self_restart")
    out = await self_restart_tool()
    assert '"status": "restarting"' in out
    assert "## self_restart failure diagnostic" not in out
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


# ---------------------------------------------------------------------------
# _ensure_url_scheme
# ---------------------------------------------------------------------------


def test_ensure_url_scheme_empty_returns_empty() -> None:
    """Empty URL is returned as-is."""
    assert _ensure_url_scheme("", "http") == ""


def test_ensure_url_scheme_http_unchanged() -> None:
    """URL with http:// scheme is returned unchanged."""
    assert (
        _ensure_url_scheme("http://central-deploy:8100", "http")
        == "http://central-deploy:8100"
    )


def test_ensure_url_scheme_https_unchanged() -> None:
    """URL with https:// scheme is returned unchanged."""
    assert (
        _ensure_url_scheme("https://central-deploy:8100", "http")
        == "https://central-deploy:8100"
    )


def test_ensure_url_scheme_no_scheme_prepends_default() -> None:
    """URL without a scheme gets the default protocol prepended."""
    assert (
        _ensure_url_scheme("central-deploy:8100", "http")
        == "http://central-deploy:8100"
    )


def test_ensure_url_scheme_no_scheme_uses_configured_protocol() -> None:
    """URL without a scheme uses the explicitly configured default protocol."""
    assert (
        _ensure_url_scheme("deploy.internal:9000", "https")
        == "https://deploy.internal:9000"
    )


def test_ensure_url_scheme_unrecognised_scheme_left_alone() -> None:
    """A URL with an unrecognised scheme (e.g. ftp://) is left unchanged."""
    assert _ensure_url_scheme("ftp://deploy:8100", "http") == "ftp://deploy:8100"


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------


def test_is_transient_error_5xx() -> None:
    """5xx HTTP errors are transient."""
    assert _is_transient_error("Lifecycle error 500 for POST url: boom") is True
    assert _is_transient_error("Lifecycle error 503 for POST url: gone") is True


def test_is_transient_error_timeout() -> None:
    """Timeout errors are transient."""
    timeout_msg = "Lifecycle request timed out after 30.0s: http://x"
    assert _is_transient_error(timeout_msg) is True


def test_is_transient_error_connection_failure() -> None:
    """Connection failures are transient."""
    assert _is_transient_error("Lifecycle request failed: connection refused") is True


def test_is_transient_error_4xx_not_transient() -> None:
    """4xx errors are not transient."""
    assert _is_transient_error("Lifecycle error 403 for POST url: nope") is False
    assert _is_transient_error("Lifecycle error 404 for POST url: missing") is False


def test_is_transient_error_success_not_transient() -> None:
    """A success response (no error marker) is not transient."""
    assert _is_transient_error('{"status": "ok"}') is False


# ---------------------------------------------------------------------------
# self_restart — retry with exponential backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_restart_succeeds_on_first_attempt(
    respx_mock: respx.MockRouter,
) -> None:
    """self_restart succeeds on the first attempt (no retries needed)."""
    respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )

    client = LifecycleClient(_settings())
    out = await client.self_restart()
    assert '"status": "restarting"' in out
    assert "## self_restart failure diagnostic" not in out


@pytest.mark.asyncio
async def test_self_restart_retries_on_503_then_succeeds(
    respx_mock: respx.MockRouter,
) -> None:
    """A 503 is retried; the method succeeds on the second attempt."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json={"status": "restarting"})

    respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        side_effect=handler
    )

    client = LifecycleClient(_settings(self_restart_max_retries=2))
    out = await client.self_restart()
    assert '"status": "restarting"' in out
    assert "## self_restart failure diagnostic" not in out
    assert call_count[0] == 2  # one retry


@pytest.mark.asyncio
async def test_self_restart_retries_on_timeout_then_succeeds(
    respx_mock: respx.MockRouter,
) -> None:
    """A timeout is retried; the method succeeds on retry."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            raise httpx.TimeoutException("timed out")
        return httpx.Response(200, json={"status": "restarting"})

    respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        side_effect=handler
    )

    client = LifecycleClient(_settings(self_restart_max_retries=2))
    out = await client.self_restart()
    assert '"status": "restarting"' in out
    assert "## self_restart failure diagnostic" not in out
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_self_restart_all_retries_exhausted(
    respx_mock: respx.MockRouter,
) -> None:
    """When all retries are exhausted, a diagnostic report is returned."""
    respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        return_value=httpx.Response(503, json={"error": "still down"})
    )

    client = LifecycleClient(_settings(self_restart_max_retries=1))
    out = await client.self_restart()
    assert "## self_restart failure diagnostic" in out
    assert "attempted **2** time(s)" in out
    assert "503" in out


@pytest.mark.asyncio
async def test_self_restart_does_not_retry_4xx(
    respx_mock: respx.MockRouter,
) -> None:
    """A 4xx error returns a diagnostic report immediately — no retries."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(403, json={"error": "forbidden"})

    respx_mock.post("http://lifecycle:9000/chat/services/chat/restart").mock(
        side_effect=handler
    )

    client = LifecycleClient(_settings(self_restart_max_retries=3))
    out = await client.self_restart()
    assert "## self_restart failure diagnostic" in out
    assert "403" in out
    assert call_count[0] == 1  # no retries


# ---------------------------------------------------------------------------
# self_restart — empty base_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_restart_empty_base_url_returns_clear_message() -> None:
    """When base_url is empty, self_restart returns a clear error message."""
    client = LifecycleClient(_settings(base_url="", default_protocol="http"))
    out = await client.self_restart()
    assert "base_url is empty" in out
    assert "http://central-deploy:8100" in out


# ---------------------------------------------------------------------------
# LifecycleClient — protocol fallback (base_url without scheme)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_no_scheme_base_url_prepends_protocol(
    respx_mock: respx.MockRouter,
) -> None:
    """When base_url has no scheme, the default protocol is prepended."""
    route = respx_mock.post(
        "http://central-deploy:8100/chat/services/chat/restart"
    ).mock(return_value=httpx.Response(200, json={"status": "restarting"}))

    client = LifecycleClient(
        _settings(base_url="central-deploy:8100", default_protocol="http")
    )
    out = await client.self_restart()
    assert '"status": "restarting"' in out
    assert "## self_restart failure diagnostic" not in out
    assert (
        route.calls.last.request.url
        == "http://central-deploy:8100/chat/services/chat/restart"
    )


@pytest.mark.asyncio
async def test_client_no_scheme_uses_https_default(
    respx_mock: respx.MockRouter,
) -> None:
    """When base_url has no scheme and default_protocol=https, https:// is prepended."""
    route = respx_mock.post(
        "https://secure-deploy:8443/chat/services/chat/restart"
    ).mock(return_value=httpx.Response(200, json={"status": "restarting"}))

    client = LifecycleClient(
        _settings(base_url="secure-deploy:8443", default_protocol="https")
    )
    out = await client.self_restart()
    assert '"status": "restarting"' in out
    assert "## self_restart failure diagnostic" not in out
    assert (
        route.calls.last.request.url
        == "https://secure-deploy:8443/chat/services/chat/restart"
    )


# ---------------------------------------------------------------------------
# _diagnose_self_restart_failure — unit tests
# ---------------------------------------------------------------------------


def test_diagnose_400_error() -> None:
    """A 400 error diagnostic references service_name configuration."""
    result = (
        "Lifecycle error 400 for POST "
        "http://deploy:8100/chat/services/chat/restart: bad request"
    )
    out = _diagnose_self_restart_failure(result, attempts=1, max_retries=3)
    assert "## self_restart failure diagnostic" in out
    assert "### What happened" in out
    assert "malformed" in out.lower()
    assert "### Next steps" in out
    assert "service_name" in out
    assert "### Raw error" in out
    assert result in out


def test_diagnose_401_error() -> None:
    """A 401 error diagnostic references the API key."""
    result = (
        "Lifecycle error 401 for POST "
        "http://deploy:8100/chat/services/chat/restart: unauthorized"
    )
    out = _diagnose_self_restart_failure(result, attempts=1, max_retries=3)
    assert "authentication failed" in out.lower()
    assert "api_key" in out.lower()


def test_diagnose_403_error() -> None:
    """A 403 error diagnostic references the per-repo access toggle."""
    result = (
        "Lifecycle error 403 for POST "
        "http://deploy:8100/chat/services/chat/restart: forbidden"
    )
    out = _diagnose_self_restart_failure(result, attempts=1, max_retries=3)
    assert "chat-agent restart toggle" in out.lower() or "chat_agent_mutatable" in out
    assert "operator" in out.lower()


def test_diagnose_404_error() -> None:
    """A 404 error diagnostic suggests running list_lifecycle_services."""
    result = (
        "Lifecycle error 404 for POST "
        "http://deploy:8100/chat/services/chat/restart: not found"
    )
    out = _diagnose_self_restart_failure(result, attempts=1, max_retries=3)
    assert "service name" in out.lower()
    assert "list_lifecycle_services" in out


def test_diagnose_timeout_error() -> None:
    """A timeout error diagnostic references network/firewall issues."""
    result = "Lifecycle request timed out after 30.0s: http://deploy:8100/chat/services/chat/restart"
    out = _diagnose_self_restart_failure(result, attempts=3, max_retries=2)
    assert "did not respond in time" in out.lower()
    assert "firewall" in out.lower()
    assert "base_url" in out
    # Multi-attempt wording.
    assert "attempted **3** time(s)" in out


def test_diagnose_connection_failure() -> None:
    """A connection/protocol failure diagnostic references URL validity."""
    result = "Lifecycle request failed: [Errno 111] Connection refused"
    out = _diagnose_self_restart_failure(result, attempts=1, max_retries=3)
    assert "could not be completed" in out.lower()
    assert "base_url" in out
    assert "http" in out.lower()


def test_diagnose_single_attempt_wording() -> None:
    """Single-attempt failures mention 'first attempt' wording."""
    result = (
        "Lifecycle error 403 for POST "
        "http://deploy:8100/chat/services/chat/restart: forbidden"
    )
    out = _diagnose_self_restart_failure(result, attempts=1, max_retries=3)
    assert "first attempt" in out.lower()
    assert "not retryable" in out.lower()


def test_diagnose_unclassified_error_fallback() -> None:
    """An unrecognised error string uses the generic fallback diagnostic."""
    result = (
        "Lifecycle error 418 for POST "
        "http://deploy:8100/chat/services/chat/restart: I'm a teapot"
    )
    out = _diagnose_self_restart_failure(result, attempts=1, max_retries=3)
    assert "unexpected error" in out.lower()
    assert "base_url" in out.lower()


# ---------------------------------------------------------------------------
# _validate_http_url
# ---------------------------------------------------------------------------


def test_validate_http_url_empty() -> None:
    """Empty string is not a valid URL."""
    assert _validate_http_url("") is False


def test_validate_http_url_http_with_host() -> None:
    """A standard http:// URL with host is valid."""
    assert _validate_http_url("http://central-deploy:8100") is True


def test_validate_http_url_https_with_host() -> None:
    """A standard https:// URL with host is valid."""
    assert _validate_http_url("https://deploy.example.com") is True


def test_validate_http_url_no_scheme() -> None:
    """A URL with no scheme is not valid."""
    assert _validate_http_url("central-deploy:8100") is False


def test_validate_http_url_relative() -> None:
    """A relative URL (/services) is not valid."""
    assert _validate_http_url("/services") is False


def test_validate_http_url_protocol_only() -> None:
    """A protocol-only stub (http://) is not valid."""
    assert _validate_http_url("http://") is False


def test_validate_http_url_unrecognised_scheme() -> None:
    """A URL with an unrecognised scheme (ftp://) is not valid."""
    assert _validate_http_url("ftp://deploy:8100") is False


def test_validate_http_url_whitespace_only() -> None:
    """A whitespace-only string is not valid."""
    assert _validate_http_url("   ") is False


def test_validate_http_url_malformed() -> None:
    """A malformed string is not a valid URL."""
    assert _validate_http_url("not a valid url at all !!!") is False


# ---------------------------------------------------------------------------
# empty / malformed base_url — list_services and other read-only methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_services_empty_base_url_returns_clear_message() -> None:
    """When base_url is empty, list_services returns a clear error message."""
    client = LifecycleClient(_settings(base_url="", default_protocol="http"))
    out = await client.list_services()
    assert "base url" in out.lower()
    assert "http://central-deploy:8100" in out


@pytest.mark.asyncio
async def test_list_services_malformed_base_url_returns_clear_message() -> None:
    """Base_url is malformed (e.g. protocol-only) → clear error message."""
    client = LifecycleClient(_settings(base_url="http://", default_protocol="http"))
    out = await client.list_services()
    assert "base url" in out.lower()
    assert "http://central-deploy:8100" in out


@pytest.mark.asyncio
async def test_service_status_empty_base_url_returns_clear_message() -> None:
    """When base_url is empty, service_status returns a clear error message."""
    client = LifecycleClient(_settings(base_url="", default_protocol="http"))
    out = await client.service_status("chat")
    assert "base url" in out.lower()
    assert "http://central-deploy:8100" in out


@pytest.mark.asyncio
async def test_service_env_empty_base_url_returns_clear_message() -> None:
    """When base_url is empty, service_env returns a clear error message."""
    client = LifecycleClient(_settings(base_url="", default_protocol="http"))
    out = await client.service_env("chat")
    assert "base url" in out.lower()
    assert "http://central-deploy:8100" in out


@pytest.mark.asyncio
async def test_restart_service_empty_base_url_returns_clear_message() -> None:
    """When base_url is empty, restart_service returns a clear error message."""
    client = LifecycleClient(_settings(base_url="", default_protocol="http"))
    out = await client.restart_service("chat")
    assert "base url" in out.lower()
    assert "http://central-deploy:8100" in out


@pytest.mark.asyncio
async def test_update_service_env_empty_base_url_returns_clear_message() -> None:
    """When base_url is empty, update_service_env returns a clear error message."""
    client = LifecycleClient(_settings(base_url="", default_protocol="http"))
    out = await client.update_service_env("chat", {"MY_VAR": "val"})
    assert "base url" in out.lower()
    assert "http://central-deploy:8100" in out


@pytest.mark.asyncio
async def test_self_restart_empty_base_url_still_uses_dedicated_guard() -> None:
    """When base_url is empty, the dedicated self_restart guard fires first.

    The existing dedicated guard (which mentions service_name) still fires
    before the generic _request guard.
    """
    client = LifecycleClient(_settings(base_url="", default_protocol="http"))
    out = await client.self_restart()
    assert "base_url is empty" in out
    assert "http://central-deploy:8100" in out


@pytest.mark.asyncio
async def test_malformed_base_url_is_treated_as_empty_for_all_methods(
    respx_mock: respx.MockRouter,
) -> None:
    """A malformed base URL causes all methods to return the clear error.

    No HTTP request is ever made (respx has no matching route).
    """
    client = LifecycleClient(_settings(base_url="http://", default_protocol="http"))
    out = await client.list_services()
    assert "base url" in out.lower()

    out = await client.service_status("chat")
    assert "base url" in out.lower()

    out = await client.service_env("chat")
    assert "base url" in out.lower()

    out = await client.restart_service("chat")
    assert "base url" in out.lower()

    out = await client.update_service_env("chat", {"MY_VAR": "val"})
    assert "base url" in out.lower()

    # Also verify no HTTP call was attempted (respx_mock would fail
    # on an unmatched route, so reaching here without error proves
    # the guard short-circuited before any request).
