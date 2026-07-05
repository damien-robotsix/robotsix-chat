r"""Central-deploy roster — fetch the allowed-chat-components list.

The roster is a JSON array of component entries returned by
``GET {central_deploy.url}/chat/components``. Each entry has:

.. code-block:: json

    {
      "id": "robotsix-mill",
      "base_url": "http://mill:8080",
      "skill": "# robotsix-mill skill\\n\\n..."
    }

The roster is cached with a short TTL (default 5 min). On failure to
reach the central-deploy API the caller gets a clear error message;
sibling resilience — no queues, no retry loops.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from robotsix_chat.config import CentralDeploySettings

logger = logging.getLogger(__name__)

# In-memory roster cache: (fetched_at_monotonic, list_of_entries).
_cache: tuple[float, list[dict[str, Any]]] | None = None

# Last non-empty roster — preserved across empty fetches as a stale fallback.
_last_non_empty_cache: tuple[float, list[dict[str, Any]]] | None = None


def _cache_valid(ttl: float) -> bool:
    """Check whether the cached roster is still fresh."""
    if _cache is None:
        return False
    fetched_at, _ = _cache
    return (time.monotonic() - fetched_at) < ttl


async def fetch_roster(
    settings: CentralDeploySettings,
) -> list[dict[str, Any]]:
    """Fetch the component roster from central-deploy.

    Returns a cached result when still fresh; on a cache miss fetches
    ``GET {url}/chat/components`` with the bearer token.

    Args:
        settings: Central-deploy configuration (url, api_token, ttl).

    Returns:
        A list of component entries (each with ``id``, ``base_url``,
        ``skill``). Returns an empty list when ``url`` is empty, and
        a list with a single error-entry when the fetch fails.

    """
    global _cache, _last_non_empty_cache
    if not settings.url:
        return []

    if _cache_valid(settings.roster_cache_ttl):
        _, entries = _cache  # type: ignore[misc]
        return entries

    token = settings.api_token.get_secret_value()
    headers: dict[str, str] = {}
    if token:
        # central-deploy's verify_auth accepts X-API-Key (or Basic) — NOT
        # Bearer. A Bearer header only ever "worked" while the deploy server
        # ran with auth disabled (exposed 2026-07-05 when an api_key was set).
        headers["X-API-Key"] = token

    roster_url = f"{settings.url.rstrip('/')}/chat/components"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(roster_url, headers=headers)
            resp.raise_for_status()
            entries = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch component roster: %s", exc)
        return [
            {
                "id": "_error",
                "base_url": "",
                "skill": "",
                "_error": f"Roster unavailable: {exc}",
            }
        ]

    if not isinstance(entries, list):
        logger.warning("Roster response is not a list: %r", type(entries))
        return []

    if not entries:
        logger.warning("Fetched component roster is empty")
        # Do not cache an empty roster for the full TTL — a transient
        # upstream blip would lock out all component_request calls.
        # Fall back to the last non-empty roster if available.
        if _last_non_empty_cache is not None:
            return _last_non_empty_cache[1]
        return []

    _cache = (time.monotonic(), entries)
    _last_non_empty_cache = _cache
    return entries


def fetch_roster_sync(settings: CentralDeploySettings) -> list[dict[str, Any]]:
    """Fetch the component roster synchronously, for startup-time use.

    Used by ``create_agent_from_settings`` to prime the roster cache and
    build the initial skill prompt before the async event loop is running.
    """
    global _cache, _last_non_empty_cache
    if not settings.url:
        return []

    if _cache_valid(settings.roster_cache_ttl):
        _, entries = _cache  # type: ignore[misc]
        return entries

    token = settings.api_token.get_secret_value()
    headers: dict[str, str] = {}
    if token:
        # central-deploy's verify_auth accepts X-API-Key (or Basic) — NOT
        # Bearer. A Bearer header only ever "worked" while the deploy server
        # ran with auth disabled (exposed 2026-07-05 when an api_key was set).
        headers["X-API-Key"] = token

    roster_url = f"{settings.url.rstrip('/')}/chat/components"
    try:
        resp = httpx.get(roster_url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        entries = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch component roster (sync): %s", exc)
        return [
            {
                "id": "_error",
                "base_url": "",
                "skill": "",
                "_error": f"Roster unavailable: {exc}",
            }
        ]

    if not isinstance(entries, list):
        logger.warning("Roster response is not a list: %r", type(entries))
        return []

    if not entries:
        logger.warning("Fetched component roster is empty (sync)")
        if _last_non_empty_cache is not None:
            return _last_non_empty_cache[1]
        return []

    _cache = (time.monotonic(), entries)
    _last_non_empty_cache = _cache
    return entries


def build_skill_prompt(entries: list[dict[str, Any]]) -> str:
    """Build a system-prompt section from the roster's skill manifests.

    Each component's skill is included verbatim. The prompt also
    includes a summary of available component ids and base URLs.

    Args:
        entries: Roster entries as returned by :func:`fetch_roster`.

    Returns:
        A string to append to the system prompt, or an empty string
        when no valid skill entries exist.

    """
    valid = [e for e in entries if e.get("skill") and not e.get("_error")]
    if not valid:
        return ""

    lines: list[str] = [
        "# Available component skills",
        "",
        "The following components are accessible via `component_request`. ",
        "Each section describes the component's API and safety rules. ",
        "",
    ]
    lines.append("## Component summary")
    lines.append("")
    for entry in valid:
        cid = entry.get("id", "?")
        base_url = entry.get("base_url", "?")
        lines.append(f"- **{cid}** — `{base_url}`")
    lines.append("")

    for entry in valid:
        cid = entry["id"]
        skill = entry["skill"]
        lines.append("---")
        lines.append(f"## {cid}")
        lines.append("")
        lines.append(skill)
        lines.append("")

    return "\n".join(lines)
