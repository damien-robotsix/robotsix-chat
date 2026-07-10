"""Tests for the notification integration.

:func:`build_notification_tools` and :func:`load_notification_skill`, with
``respx`` mocked so there are no real network calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import NotificationSettings
from robotsix_chat.notification import (
    build_notification_tools,
    load_notification_skill,
)


def _settings(**kw: Any) -> NotificationSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "ntfy_topic": "test-topic",
    }
    base.update(kw)
    return NotificationSettings(**base)


# ---------------------------------------------------------------------------
# load_notification_skill
# ---------------------------------------------------------------------------


def test_load_notification_skill_returns_non_empty() -> None:
    """The bundled skill.md is readable and non-empty."""
    skill = load_notification_skill()
    assert len(skill) > 0
    assert "notify_user" in skill


# ---------------------------------------------------------------------------
# build_notification_tools
# ---------------------------------------------------------------------------


def test_build_disabled_returns_empty() -> None:
    """Disabled notification returns no tools."""
    assert build_notification_tools(NotificationSettings(enabled=False)) == []


def test_build_enabled_returns_one_tool() -> None:
    """Enabled notification returns a single callable named notify_user."""
    tools = build_notification_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "notify_user"


# ---------------------------------------------------------------------------
# notify_user — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_user_success(respx_mock: respx.MockRouter) -> None:
    """A successful push returns 'Notification sent.'."""
    route = respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(200, json={"id": "abc123"})
    )

    tools = build_notification_tools(_settings())
    result = await tools[0](
        title="Test",
        body="Test body",
        urgency="default",
    )

    assert result == "Notification sent."
    assert route.called


@pytest.mark.asyncio
async def test_notify_user_with_link(respx_mock: respx.MockRouter) -> None:
    """A notification with a link includes the click field in the payload."""
    route = respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(200, json={})
    )

    tools = build_notification_tools(_settings())
    await tools[0](
        title="PR merged",
        body="PR #42 was merged.",
        urgency="default",
        link="https://github.com/org/repo/pull/42",
    )

    assert route.called
    payload = json.loads(route.calls.last.request.content.decode())
    assert payload["click"] == "https://github.com/org/repo/pull/42"


@pytest.mark.asyncio
async def test_notify_user_urgency_high(respx_mock: respx.MockRouter) -> None:
    """High urgency maps to 'high' priority in the ntfy payload."""
    route = respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(200, json={})
    )

    tools = build_notification_tools(_settings())
    await tools[0](
        title="Urgent",
        body="Something needs attention",
        urgency="high",
    )

    assert route.called
    payload = json.loads(route.calls.last.request.content.decode())
    assert payload["priority"] == "high"


@pytest.mark.asyncio
async def test_notify_user_urgency_low(respx_mock: respx.MockRouter) -> None:
    """Low urgency maps to 'low' priority in the ntfy payload."""
    route = respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(200, json={})
    )

    tools = build_notification_tools(_settings())
    await tools[0](
        title="Routine",
        body="Routine check completed",
        urgency="low",
    )

    assert route.called
    payload = json.loads(route.calls.last.request.content.decode())
    assert payload["priority"] == "low"


@pytest.mark.asyncio
async def test_notify_user_invalid_urgency_falls_back(
    respx_mock: respx.MockRouter,
) -> None:
    """An invalid urgency value falls back to 'default'."""
    route = respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(200, json={})
    )

    tools = build_notification_tools(_settings())
    await tools[0](
        title="Test",
        body="Test",
        urgency="critical",  # invalid — should fall back
    )

    payload = json.loads(route.calls.last.request.content.decode())
    assert payload["priority"] == "default"


# ---------------------------------------------------------------------------
# notify_user — error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_user_http_error(respx_mock: respx.MockRouter) -> None:
    """When the server returns an error, the tool returns a failure message."""
    respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(403, json={})
    )

    tools = build_notification_tools(_settings())
    result = await tools[0](
        title="Test",
        body="Test",
    )

    assert "failed" in result.lower()
    assert "403" in result


@pytest.mark.asyncio
async def test_notify_user_timeout(respx_mock: respx.MockRouter) -> None:
    """A timeout returns a failure message — never raises."""
    respx_mock.post("https://ntfy.sh/test-topic").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    tools = build_notification_tools(_settings())
    result = await tools[0](
        title="Test",
        body="Test",
    )

    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_notify_user_unexpected_error(
    respx_mock: respx.MockRouter,
) -> None:
    """An unexpected exception returns a failure message — never raises."""
    respx_mock.post("https://ntfy.sh/test-topic").mock(
        side_effect=RuntimeError("something crashed")
    )

    tools = build_notification_tools(_settings())
    result = await tools[0](
        title="Test",
        body="Test",
    )

    assert "unexpected error" in result.lower()


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_auth_header_when_token_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """When no token is configured, no Authorization header is sent."""
    route = respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(200, json={})
    )

    tools = build_notification_tools(_settings())
    await tools[0](title="Test", body="Test")

    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_auth_header_when_token_configured(
    respx_mock: respx.MockRouter,
) -> None:
    """When a token is configured, Bearer auth is sent."""
    route = respx_mock.post("https://ntfy.sh/test-topic").mock(
        return_value=httpx.Response(200, json={})
    )

    tools = build_notification_tools(
        _settings(ntfy_token="tk_test123")  # pragma: allowlist secret
    )
    await tools[0](title="Test", body="Test")

    assert (
        route.calls.last.request.headers["authorization"]
        == "Bearer tk_test123"  # pragma: allowlist secret
    )


# ---------------------------------------------------------------------------
# Custom server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_ntfy_server(respx_mock: respx.MockRouter) -> None:
    """A custom ntfy_server is used for the POST URL."""
    route = respx_mock.post("https://ntfy.example.com/my-topic").mock(
        return_value=httpx.Response(200, json={})
    )

    tools = build_notification_tools(
        _settings(
            ntfy_topic="my-topic",
            ntfy_server="https://ntfy.example.com",
        )
    )
    await tools[0](title="Test", body="Test")

    assert route.called


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_settings_enabled_topic_required() -> None:
    """When enabled and ntfy_topic is empty, Settings raises ValueError."""
    from robotsix_chat.config import Settings

    with pytest.raises(ValueError, match="notification.ntfy_topic"):
        Settings(
            notification={"enabled": True, "ntfy_topic": ""},  # type: ignore[arg-type]
        )
