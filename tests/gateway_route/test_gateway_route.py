"""Tests for the gateway-route diagnostic tool.

:func:`build_gateway_route_tools` with ``respx`` mocked so there are no
real network calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import CentralDeploySettings, GatewayRouteSettings
from robotsix_chat.gateway_route import (
    build_gateway_route_tools,
    load_gateway_route_skill,
)

_REGISTRY_URL = "http://cd:8100/components/suggest"


def _settings(**kw: Any) -> GatewayRouteSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "timeout": 30.0,
        "gateway_base_domain": "deploy.robotsix.net",
    }
    base.update(kw)
    return GatewayRouteSettings(**base)


def _central_deploy(**kw: Any) -> CentralDeploySettings:
    base: dict[str, Any] = {
        "url": "http://cd:8100",
    }
    base.update(kw)
    return CentralDeploySettings(**base)


def _components_payload(*components: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"components": list(components)})


# ---------------------------------------------------------------------------
# build_gateway_route_tools
# ---------------------------------------------------------------------------


def test_build_gateway_route_tools_disabled() -> None:
    """Disabled gateway_route returns no tools."""
    assert build_gateway_route_tools(GatewayRouteSettings(enabled=False)) == []


def test_build_gateway_route_tools_returns_one_tool() -> None:
    """Enabled gateway_route returns exactly one tool: check_gateway_route."""
    tools = build_gateway_route_tools(_settings(), _central_deploy())
    assert len(tools) == 1
    assert tools[0].__name__ == "check_gateway_route"


# ---------------------------------------------------------------------------
# load_gateway_route_skill
# ---------------------------------------------------------------------------


def test_load_gateway_route_skill_returns_non_empty_markdown() -> None:
    """The shipped skill.md is loadable and describes the tool."""
    skill = load_gateway_route_skill()
    assert len(skill) > 100
    assert "check_gateway_route" in skill
    assert "read-only" in skill.lower()


# ---------------------------------------------------------------------------
# check_gateway_route tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_gateway_route_route_present(
    respx_mock: respx.MockRouter,
) -> None:
    """A slug present in the registry reports route_present=True."""
    route = respx_mock.get(_REGISTRY_URL).mock(
        return_value=_components_payload(
            {
                "id": "robotsix-chat",
                "container_name": "robotsix-chat",
                "container_port": 8080,
            },
            {
                "id": "robotsix-mill",
                "container_name": "mill",
                "container_port": 8080,
            },
        )
    )

    tools = build_gateway_route_tools(_settings(), _central_deploy())
    result = json.loads(await tools[0]("robotsix-chat"))

    assert route.called
    assert result["error"] == ""
    assert result["expected_route"] == "robotsix-chat.deploy.robotsix.net"
    assert result["route_present"] is True
    assert len(result["matching_mappings"]) == 1
    assert (
        result["matching_mappings"][0]["vhost"] == "robotsix-chat.deploy.robotsix.net"
    )
    assert result["matching_mappings"][0]["upstream"] == "robotsix-chat:8080"
    assert len(result["current_mappings"]) == 2
    assert "present" in result["diagnosis"]


@pytest.mark.asyncio
async def test_check_gateway_route_route_missing(
    respx_mock: respx.MockRouter,
) -> None:
    """A slug absent from the registry reports route_present=False."""
    respx_mock.get(_REGISTRY_URL).mock(
        return_value=_components_payload(
            {
                "id": "robotsix-mill",
                "container_name": "mill",
                "container_port": 8080,
            }
        )
    )

    tools = build_gateway_route_tools(_settings(), _central_deploy())
    result = json.loads(await tools[0]("robotsix-chat"))

    assert result["error"] == ""
    assert result["route_present"] is False
    assert result["matching_mappings"] == []
    assert result["expected_route"] == "robotsix-chat.deploy.robotsix.net"
    assert "No gateway route" in result["diagnosis"]


@pytest.mark.asyncio
async def test_check_gateway_route_invalid_slug_makes_no_request(
    respx_mock: respx.MockRouter,
) -> None:
    """An invalid slug is rejected before any HTTP request is made."""
    tools = build_gateway_route_tools(_settings(), _central_deploy())
    result = json.loads(await tools[0]("../../etc/passwd"))

    assert result["error"] != ""
    assert result["route_present"] is False
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_check_gateway_route_unconfigured_central_deploy() -> None:
    """A missing central-deploy URL is reported as a clear error."""
    tools = build_gateway_route_tools(_settings(), _central_deploy(url=""))
    result = json.loads(await tools[0]("robotsix-chat"))

    assert "central_deploy.url" in result["error"]
    assert result["route_present"] is False


@pytest.mark.asyncio
async def test_check_gateway_route_registry_error(
    respx_mock: respx.MockRouter,
) -> None:
    """A non-2xx registry response surfaces the HTTP error."""
    respx_mock.get(_REGISTRY_URL).mock(return_value=httpx.Response(500))

    tools = build_gateway_route_tools(_settings(), _central_deploy())
    result = json.loads(await tools[0]("robotsix-chat"))

    assert result["error"] != ""
    assert result["route_present"] is False


@pytest.mark.asyncio
async def test_check_gateway_route_invalid_registry_json(
    respx_mock: respx.MockRouter,
) -> None:
    """A non-JSON registry response is reported instead of crashing."""
    respx_mock.get(_REGISTRY_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )

    tools = build_gateway_route_tools(_settings(), _central_deploy())
    result = json.loads(await tools[0]("robotsix-chat"))

    assert "invalid JSON" in result["error"]
    assert result["route_present"] is False
