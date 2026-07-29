"""Tests for the Docker digest resolution tool.

:func:`build_docker_digest_tools` with ``respx`` mocked so there are
no real network calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DockerDigestSettings
from robotsix_chat.docker_digest import (
    build_docker_digest_tools,
    load_docker_digest_skill,
)


def _settings(**kwargs: Any) -> DockerDigestSettings:
    defaults: dict[str, Any] = {"enabled": True, "timeout": 30.0}
    defaults.update(kwargs)
    return DockerDigestSettings(**defaults)


# ---------------------------------------------------------------------------
# build_docker_digest_tools — factory behaviour
# ---------------------------------------------------------------------------


def test_build_returns_empty_list_when_disabled() -> None:
    """When settings.enabled=False, build returns an empty list."""
    assert build_docker_digest_tools(DockerDigestSettings(enabled=False)) == []


def test_build_returns_tool_when_enabled() -> None:
    """When settings.enabled=True, build returns a list with one callable."""
    tools = build_docker_digest_tools(_settings())
    assert len(tools) == 1
    assert tools[0].__name__ == "resolve_docker_digest"


# ---------------------------------------------------------------------------
# load_docker_digest_skill
# ---------------------------------------------------------------------------


def test_load_skill_returns_content() -> None:
    """The shipped skill.md is loadable and describes the tool."""
    skill = load_docker_digest_skill()
    assert len(skill) > 100
    assert "resolve_docker_digest" in skill
    assert "read-only" in skill.lower()


# ---------------------------------------------------------------------------
# resolve_docker_digest — Docker Hub official image
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_docker_hub_official_image(
    respx_mock: respx.MockRouter,
) -> None:
    """Resolve a Docker Hub official image (library namespace)."""
    # Mock the auth token endpoint
    token_route = respx_mock.get(
        "https://auth.docker.io/token",
        params={
            "service": "registry.docker.io",
            "scope": "repository:library/python:pull",
        },
    ).mock(return_value=httpx.Response(200, json={"token": "fake-token"}))

    # Mock the manifest endpoint
    manifest_route = respx_mock.get(
        "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim",
    ).mock(
        return_value=httpx.Response(
            200,
            headers={
                "Docker-Content-Digest": "sha256:abc123def456789",
                "Content-Type": (
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
            json={
                "schemaVersion": 2,
                "mediaType": ("application/vnd.docker.distribution.manifest.v2+json"),
                "config": {
                    "mediaType": "application/vnd.docker.container.image.v1+json",
                    "digest": "sha256:configdigest123",
                },
            },
        )
    )

    tools = build_docker_digest_tools(_settings())
    result = json.loads(await tools[0]("python:3.14-slim"))

    assert token_route.called
    assert manifest_route.called
    assert result["digest"] == "sha256:abc123def456789"
    assert result["resolved_ref"] == "python@sha256:abc123def456789"
    assert result["image"] == "python:3.14-slim"
    assert result["platform"] == "linux/amd64"
    assert result["error"] == ""
    assert "manifest.v2" in result["media_type"]


# ---------------------------------------------------------------------------
# resolve_docker_digest — Docker Hub with namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_docker_hub_with_namespace(
    respx_mock: respx.MockRouter,
) -> None:
    """Resolve a Docker Hub image with user/org namespace."""
    token_route = respx_mock.get(
        "https://auth.docker.io/token",
        params={
            "service": "registry.docker.io",
            "scope": "repository:myuser/myimage:pull",
        },
    ).mock(return_value=httpx.Response(200, json={"token": "fake-token"}))

    manifest_route = respx_mock.get(
        "https://registry-1.docker.io/v2/myuser/myimage/manifests/latest",
    ).mock(
        return_value=httpx.Response(
            200,
            headers={
                "Docker-Content-Digest": "sha256:namespace123abc",
                "Content-Type": (
                    "application/vnd.docker.distribution.manifest.v2+json"
                ),
            },
            json={"schemaVersion": 2, "config": {}},
        )
    )

    tools = build_docker_digest_tools(_settings())
    result = json.loads(await tools[0]("myuser/myimage:latest"))

    assert token_route.called
    assert manifest_route.called
    assert result["digest"] == "sha256:namespace123abc"
    assert result["resolved_ref"] == "myuser/myimage@sha256:namespace123abc"
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# resolve_docker_digest — manifest list resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_with_manifest_list(
    respx_mock: respx.MockRouter,
) -> None:
    """Resolve the platform-specific sub-manifest from a manifest list."""
    # Auth
    respx_mock.get(
        "https://auth.docker.io/token",
        params={
            "service": "registry.docker.io",
            "scope": "repository:library/python:pull",
        },
    ).mock(return_value=httpx.Response(200, json={"token": "fake-token"}))

    # Manifest list endpoint — returns a multi-arch list
    respx_mock.get(
        "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim",
    ).mock(
        return_value=httpx.Response(
            200,
            headers={
                "Content-Type": (
                    "application/vnd.docker.distribution.manifest.list.v2+json"
                ),
            },
            json={
                "schemaVersion": 2,
                "mediaType": (
                    "application/vnd.docker.distribution.manifest.list.v2+json"
                ),
                "manifests": [
                    {
                        "mediaType": (
                            "application/vnd.docker.distribution.manifest.v2+json"
                        ),
                        "digest": "sha256:amd64manifestdigest123",
                        "platform": {
                            "architecture": "amd64",
                            "os": "linux",
                        },
                    },
                    {
                        "mediaType": (
                            "application/vnd.docker.distribution.manifest.v2+json"
                        ),
                        "digest": "sha256:arm64manifestdigest456",
                        "platform": {
                            "architecture": "arm64",
                            "os": "linux",
                            "variant": "v8",
                        },
                    },
                ],
            },
        )
    )

    tools = build_docker_digest_tools(_settings())
    result = json.loads(await tools[0]("python:3.14-slim", platform="linux/amd64"))

    # The entry's digest field IS the content digest — no sub-manifest fetch needed (§2 step 4).
    assert result["digest"] == "sha256:amd64manifestdigest123"
    assert result["resolved_ref"] == "python@sha256:amd64manifestdigest123"
    assert result["platform"] == "linux/amd64"
    assert result["error"] == ""


# ---------------------------------------------------------------------------
# Platform not found in manifest list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_not_found_in_manifest_list(
    respx_mock: respx.MockRouter,
) -> None:
    """Return an error when the requested platform is not in the manifest list."""
    respx_mock.get(
        "https://auth.docker.io/token",
        params={
            "service": "registry.docker.io",
            "scope": "repository:library/python:pull",
        },
    ).mock(return_value=httpx.Response(200, json={"token": "fake-token"}))

    respx_mock.get(
        "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim",
    ).mock(
        return_value=httpx.Response(
            200,
            headers={
                "Content-Type": (
                    "application/vnd.docker.distribution.manifest.list.v2+json"
                ),
            },
            json={
                "schemaVersion": 2,
                "mediaType": (
                    "application/vnd.docker.distribution.manifest.list.v2+json"
                ),
                "manifests": [
                    {
                        "mediaType": (
                            "application/vnd.docker.distribution.manifest.v2+json"
                        ),
                        "digest": "sha256:amd64only",
                        "platform": {
                            "architecture": "amd64",
                            "os": "linux",
                        },
                    },
                ],
            },
        )
    )

    tools = build_docker_digest_tools(_settings())
    result = json.loads(await tools[0]("python:3.14-slim", platform="linux/arm64"))

    assert result["digest"] == ""
    assert result["resolved_ref"] == ""
    assert "No manifest found" in result["error"]
    assert "linux/arm64" in result["error"]


# ---------------------------------------------------------------------------
# HTTP error from registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error(respx_mock: respx.MockRouter) -> None:
    """A 404 from the registry populates the error field."""
    respx_mock.get(
        "https://auth.docker.io/token",
        params={
            "service": "registry.docker.io",
            "scope": "repository:library/python:pull",
        },
    ).mock(return_value=httpx.Response(200, json={"token": "fake-token"}))

    respx_mock.get(
        "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim",
    ).mock(return_value=httpx.Response(404, text="not found"))

    tools = build_docker_digest_tools(_settings())
    result = json.loads(await tools[0]("python:3.14-slim"))

    assert result["digest"] == ""
    assert result["resolved_ref"] == ""
    assert "404" in result["error"]


# ---------------------------------------------------------------------------
# Timeout error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_error(respx_mock: respx.MockRouter) -> None:
    """A timeout populates the error field."""
    respx_mock.get(
        "https://auth.docker.io/token",
        params={
            "service": "registry.docker.io",
            "scope": "repository:library/python:pull",
        },
    ).mock(return_value=httpx.Response(200, json={"token": "fake-token"}))

    respx_mock.get(
        "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim",
    ).mock(side_effect=httpx.TimeoutException("timed out"))

    tools = build_docker_digest_tools(_settings())
    result = json.loads(await tools[0]("python:3.14-slim"))

    assert result["digest"] == ""
    assert result["resolved_ref"] == ""
    assert "timed out" in result["error"].lower()
