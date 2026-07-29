"""Read-only Docker digest resolution tool for the agent.

Resolves a Docker image reference (e.g. ``python:3.14-slim``) and a target
platform to its immutable ``sha256:...`` content digest by querying the
Docker Registry v2 HTTP API.  Supports Docker Hub and third-party registries
(GHCR, etc.) via anonymous/public token exchange.

Safe by construction: read-only HTTP GET, no credentials, one-image-per-call,
configurable timeout.  Private registries will return an authentication error.

Exposes :func:`build_docker_digest_tools` — a factory returning the LLM tool.
Returns no tools when disabled, so the chat runs exactly as before.  Also
exposes :func:`load_docker_digest_skill` which returns the component skill
markdown for injection into the agent instruction.
"""

from __future__ import annotations

import json
import logging
import re as _re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import DockerDigestSettings

__all__ = ["build_docker_digest_tools", "load_docker_digest_skill"]

logger = logging.getLogger(__name__)


def load_docker_digest_skill() -> str:
    """Return the docker-digest component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_docker_digest_tools(
    settings: DockerDigestSettings,
) -> list[Callable[..., Any]]:
    """Return the ``resolve_docker_digest`` tool, or an empty list when disabled.

    Args:
        settings: DockerDigest configuration (``enabled`` master switch,
            ``timeout``).

    Returns:
        A single-element list containing the ``resolve_docker_digest`` async
        callable, or ``[]`` when *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    async def resolve_docker_digest(
        image: str,
        platform: str = "linux/amd64",
    ) -> str:
        """Resolve a Docker image tag to its immutable content digest.

        Queries the Docker Registry v2 HTTP API to fetch the manifest for
        *image* and returns the ``Docker-Content-Digest`` for the requested
        *platform*.  Supports Docker Hub and third-party registries (GHCR,
        etc.) via anonymous/public token exchange.

        Args:
            image: Docker image reference.  Accepts ``image:tag``
                (e.g. ``python:3.14-slim``), ``registry/namespace/repo:tag``
                (e.g. ``ghcr.io/owner/repo:main``), and bare ``image``
                (defaults to ``:latest``).
            platform: Target platform in ``os/arch`` format
                (default ``linux/amd64``).  Examples: ``linux/amd64``,
                ``linux/arm64``, ``linux/arm/v7``.

        Returns:
            A JSON string with ``image``, ``platform``, ``digest``,
            ``resolved_ref``, ``media_type``, and ``error`` fields.

        """
        result: dict[str, Any] = {
            "image": image,
            "platform": platform,
            "digest": "",
            "resolved_ref": "",
            "media_type": "",
            "error": "",
        }

        # --- @sha256: shortcut ---
        if "@sha256:" in image:
            raw_image, raw_digest_suffix = image.split("@sha256:", 1)
            resolved_digest = "sha256:" + raw_digest_suffix
            result["digest"] = resolved_digest
            result["resolved_ref"] = f"{raw_image}@{resolved_digest}"
            return json.dumps(result, ensure_ascii=False)

        # --- Parse image reference ---
        image_without_tag: str
        tag: str
        if ":" in image:
            last_colon = image.rfind(":")
            image_without_tag = image[:last_colon]
            tag = image[last_colon + 1 :]
            if not tag:
                tag = "latest"
        else:
            image_without_tag = image
            tag = "latest"

        # --- Determine registry, namespace, repo ---
        parts = image_without_tag.split("/")
        first_part = parts[0]
        is_docker_hub = (
            "." not in first_part
            and ":" not in first_part
            and first_part != "localhost"
        )

        if is_docker_hub:
            registry_host = settings.registry_host
            if len(parts) == 1:
                namespace = "library"
                repo = parts[0]
            else:
                namespace = parts[0]
                repo = "/".join(parts[1:])
        else:
            registry_host = first_part
            if len(parts) == 1:
                # Bare registry with no namespace — shouldn't happen but handle
                result["error"] = (
                    f"Invalid image reference {image!r}: missing namespace/repo "
                    "for third-party registry"
                )
                return json.dumps(result, ensure_ascii=False)
            namespace = parts[1]
            repo = "/".join(parts[2:]) if len(parts) > 2 else ""

        if not repo:
            result["error"] = (
                f"Invalid image reference {image!r}: cannot determine repository"
            )
            return json.dumps(result, ensure_ascii=False)

        repo_path = f"{namespace}/{repo}"

        # --- Auth token ---
        token: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout,
                follow_redirects=True,
            ) as client:
                if is_docker_hub:
                    token_url = (
                        f"{settings.auth_url}"
                        f"?service=registry.docker.io"
                        f"&scope=repository:{repo_path}:pull"
                    )
                    token_resp = await client.get(token_url)
                    token_resp.raise_for_status()
                    token_data = token_resp.json()
                    token = token_data.get("token", "")
        except Exception as exc:
            logger.warning("Failed to obtain auth token for %s: %s", image, exc)
            # Continue without token — some registries allow anonymous pulls

        # --- Fetch manifest ---
        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout,
                follow_redirects=True,
            ) as client:
                manifest_url = f"https://{registry_host}/v2/{repo_path}/manifests/{tag}"

                _accept = (
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json,"
                    "application/vnd.oci.image.manifest.v1+json"
                )
                headers: dict[str, str] = {"Accept": _accept}
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                manifest_resp = await client.get(manifest_url, headers=headers)

                # On 401 for non-Docker-Hub registries, try the registry's
                # own auth endpoint via the Www-Authenticate challenge.
                if manifest_resp.status_code == 401 and not is_docker_hub and not token:
                    www_auth = manifest_resp.headers.get("Www-Authenticate", "")
                    token = await _fetch_token_from_challenge(
                        client, www_auth, repo_path
                    )
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        manifest_resp = await client.get(manifest_url, headers=headers)

                manifest_resp.raise_for_status()

                digest: str = manifest_resp.headers.get("Docker-Content-Digest", "")
                media_type: str = manifest_resp.headers.get("Content-Type", "")
                manifest_body = manifest_resp.json()

                # If it's a manifest list, find the platform-specific entry
                _body_mt: str = (
                    manifest_body.get("mediaType", "")
                    if isinstance(manifest_body, dict)
                    else ""
                )
                is_manifest_list = (
                    media_type
                    and ("manifest.list" in media_type or "image.index" in media_type)
                ) or ("manifest.list" in _body_mt or "image.index" in _body_mt)
                if is_manifest_list:
                    manifests = manifest_body.get("manifests", [])
                    platform_os, platform_arch, platform_variant = _parse_platform(
                        platform
                    )

                    found = False
                    for entry in manifests:
                        entry_platform = entry.get("platform", {})
                        entry_os = entry_platform.get("os", "")
                        entry_arch = entry_platform.get("architecture", "")
                        entry_variant = entry_platform.get("variant", "")

                        if entry_os != platform_os or entry_arch != platform_arch:
                            continue
                        if (
                            platform_variant
                            and entry_variant
                            and entry_variant != platform_variant
                        ):
                            continue
                        if platform_variant and not entry_variant:
                            continue

                        found = True
                        digest = entry.get("digest", "")
                        break

                    if not found:
                        available = [e.get("platform", {}) for e in manifests]
                        result["error"] = (
                            f"No manifest found for platform {platform} in the "
                            f"manifest list for {image}. "
                            f"Available platforms: {available}"
                        )
                        return json.dumps(result, ensure_ascii=False)

                result["digest"] = digest
                result["media_type"] = media_type
                if digest:
                    result["resolved_ref"] = f"{image_without_tag}@{digest}"

        except httpx.TimeoutException:
            result["error"] = f"Request timed out after {settings.timeout}s for {image}"
            logger.warning("docker_digest timed out for %s", image)
        except httpx.HTTPStatusError as exc:
            result["error"] = (
                f"Registry returned HTTP {exc.response.status_code} for "
                f"{image}: {exc.response.text[:500]}"
            )
            logger.warning("docker_digest HTTP error for %s: %s", image, exc)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("docker_digest failed for %s", image)

        return json.dumps(result, ensure_ascii=False)

    return [resolve_docker_digest]


def _parse_platform(platform: str) -> tuple[str, str, str]:
    """Parse a ``os/arch[/variant]`` platform string.

    ``linux/amd64``  → (``linux``, ``amd64``, ``""``)
    ``linux/arm/v7`` → (``linux``, ``arm``, ``v7``)

    """
    parts = platform.split("/", 2)
    os_name = parts[0]
    arch = parts[1] if len(parts) > 1 else "amd64"
    variant = parts[2] if len(parts) > 2 else ""
    return os_name, arch, variant


async def _fetch_token_from_challenge(
    client: httpx.AsyncClient,
    www_authenticate: str,
    repo_path: str,
) -> str | None:
    """Parse a ``Bearer`` challenge and fetch a token from the realm endpoint.

    Example ``Www-Authenticate`` header::

        Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:owner/repo:pull"

    Returns the bearer token string, or ``None`` if the challenge cannot be
    parsed or the token endpoint returns an error.

    """
    if not www_authenticate:
        return None

    realm_match = _re.search(r'realm="([^"]+)"', www_authenticate)
    if not realm_match:
        return None

    realm = realm_match.group(1)
    service_match = _re.search(r'service="([^"]+)"', www_authenticate)

    # Build token URL
    separator = "&" if "?" in realm else "?"
    token_url = f"{realm}{separator}scope=repository:{repo_path}:pull"
    if service_match:
        token_url += f"&service={service_match.group(1)}"

    try:
        token_resp = await client.get(token_url)
        token_resp.raise_for_status()
        token_data: dict[str, object] = token_resp.json()
        token: object = token_data.get("token") or token_data.get("access_token")
        if isinstance(token, str):
            return token
        return None
    except Exception as exc:
        logger.warning("Failed to fetch token from challenge: %s", exc)
        return None
