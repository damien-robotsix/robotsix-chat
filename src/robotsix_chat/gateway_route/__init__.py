"""Gateway-route diagnostic tool for the agent.

The central-deploy edge derives its routing table from the component
registry: every registered component with ``id == <slug>`` is published
at ``<slug>.<gateway_base_domain>``, and the registry entry's container
name/port is the upstream.  There is no per-service routing rule — a
route "exists" exactly when the slug is present in the registry.

This module exposes :func:`build_gateway_route_tools` — a factory that
returns the ``check_gateway_route`` tool.  The tool fetches the registry
(``GET /components/suggest``), turns it into the current
vhost → upstream mapping, computes the expected route for the supplied
slug, and reports whether the route is present.  Returns no tools when
disabled, so the chat runs exactly as before.  Also exposes
:func:`load_gateway_route_skill` which returns the component skill
markdown for injection into the agent instruction.

The registry response shape is ``{"components": [{"id", "container_name",
"container_port"}, ...]}`` — see central-deploy's
``GET /components/suggest`` endpoint (``ComponentSuggestResponse``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import CentralDeploySettings, GatewayRouteSettings

__all__ = ["build_gateway_route_tools", "load_gateway_route_skill"]

# Mirrors central-deploy's ``ComponentConfig.id`` pattern — a stable,
# lowercase DNS-like slug.  Rejecting anything else up front keeps the
# slug out of the returned report strings and avoids confusing lookups.
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def load_gateway_route_skill() -> str:
    """Return the gateway-route component skill markdown.

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


def _registry_url(central_deploy: CentralDeploySettings) -> str:
    """Return the absolute URL of central-deploy's component registry."""
    return f"{central_deploy.url.rstrip('/')}/components/suggest"


def _mapping_for(
    component: dict[str, Any],
    gateway_base_domain: str,
) -> dict[str, Any]:
    """Build one ``{vhost, upstream, component_id}`` mapping entry."""
    component_id = str(component.get("id", ""))
    container_name = str(component.get("container_name", ""))
    container_port = component.get("container_port")

    vhost = f"{component_id}.{gateway_base_domain}" if gateway_base_domain else ""
    if container_name:
        upstream = (
            f"{container_name}:{container_port}"
            if container_port is not None
            else container_name
        )
    else:
        upstream = component_id or "(unknown)"

    return {
        "component_id": component_id,
        "vhost": vhost,
        "upstream": upstream,
    }


def build_gateway_route_tools(
    settings: GatewayRouteSettings,
    central_deploy: CentralDeploySettings | None = None,
) -> list[Callable[..., Any]]:
    """Return the ``check_gateway_route`` tool, or an empty list when disabled.

    Args:
        settings: Gateway-route configuration (``enabled`` master switch,
            request timeout, and the fleet ``gateway_base_domain`` used to
            derive ``<slug>.<base_domain>`` vhosts).
        central_deploy: Central-deploy connection settings (base URL and
            API token).  The tool reads the component registry at
            ``GET {url}/components/suggest`` and never mutates anything.

    Returns:
        A single-element list containing the ``check_gateway_route`` async
        callable, or ``[]`` when *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    async def check_gateway_route(service_slug: str) -> str:
        """Check whether a service has an active edge-gateway route.

        Fetches the central-deploy component registry and compares it with
        the expected route for *service_slug* (derived as
        ``<service_slug>.<gateway_base_domain>`` — central-deploy publishes
        every registered, routable component at that vhost automatically).

        Args:
            service_slug: The service/component id to check (lowercase
                DNS-like slug, e.g. ``robotsix-chat``).

        Returns:
            A JSON string with ``service_slug``, ``gateway_base_domain``,
            ``expected_route``, ``route_present`` (bool),
            ``matching_mappings`` (registry entries whose id matches the
            slug), ``current_mappings`` (the full vhost → upstream table),
            ``diagnosis`` (a human-readable conclusion), and ``error``
            (non-empty when the registry could not be read or the slug is
            invalid).

        """
        result: dict[str, Any] = {
            "service_slug": service_slug,
            "gateway_base_domain": settings.gateway_base_domain,
            "expected_route": "",
            "route_present": False,
            "matching_mappings": [],
            "current_mappings": [],
            "diagnosis": "",
            "error": "",
        }

        # --- Slug validation -------------------------------------------------
        if not service_slug or not _SLUG_PATTERN.fullmatch(service_slug):
            result["error"] = (
                f"Invalid service slug {service_slug!r}: expected a lowercase "
                "DNS-like id matching ^[a-z0-9][a-z0-9-]*$."
            )
            return json.dumps(result, ensure_ascii=False)

        base_domain = settings.gateway_base_domain.strip()
        result["expected_route"] = (
            f"{service_slug}.{base_domain}" if base_domain else ""
        )
        if not base_domain:
            result["error"] = (
                "gateway_route.gateway_base_domain is not configured — "
                "cannot derive the expected vhost."
            )
            return json.dumps(result, ensure_ascii=False)

        if central_deploy is None or not central_deploy.url.strip():
            result["error"] = (
                "central_deploy.url is not configured — cannot read the "
                "component registry."
            )
            return json.dumps(result, ensure_ascii=False)

        # --- Fetch the registry ----------------------------------------------
        headers: dict[str, str] = {}
        token = central_deploy.api_token.get_secret_value()
        if token:
            headers["X-API-Key"] = token

        response = await safe_http_request(
            "GET",
            _registry_url(central_deploy),
            headers=headers,
            timeout=settings.timeout,
            label="Gateway route",
        )
        if response.error is not None:
            result["error"] = response.error
            return json.dumps(result, ensure_ascii=False)

        try:
            payload = json.loads(response.text or "{}")
        except json.JSONDecodeError as exc:
            result["error"] = f"Gateway route registry returned invalid JSON: {exc}"
            return json.dumps(result, ensure_ascii=False)

        components = payload.get("components") if isinstance(payload, dict) else None
        if not isinstance(components, list):
            result["error"] = (
                "Gateway route registry response has no 'components' list: "
                f"{(response.text or '')[:200]!r}"
            )
            return json.dumps(result, ensure_ascii=False)

        # --- Build current vhost → upstream mapping --------------------------
        mappings = [
            _mapping_for(component, base_domain)
            for component in components
            if isinstance(component, dict)
        ]
        result["current_mappings"] = mappings

        matching = [
            mapping for mapping in mappings if mapping["component_id"] == service_slug
        ]
        result["matching_mappings"] = matching
        result["route_present"] = bool(matching)

        if matching:
            result["diagnosis"] = (
                f"Route {result['expected_route']} is present — the service is "
                f"registered and the edge publishes it upstream to "
                f"{matching[0]['upstream']}."
            )
        else:
            result["diagnosis"] = (
                f"No gateway route for {result['expected_route']}: "
                f"{service_slug!r} is not present in the component registry. "
                "The service is either not onboarded in central-deploy or is a "
                "non-routable sibling — the edge derives no vhost for it."
            )

        return json.dumps(result, ensure_ascii=False)

    return [check_gateway_route]
