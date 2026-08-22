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
from robotsix_http import RetryClient, RetryConfig

from robotsix_chat.config import CentralDeploySettings

logger = logging.getLogger(__name__)

# In-memory roster cache: (fetched_at_monotonic, list_of_entries).
_cache: tuple[float, list[dict[str, Any]]] | None = None

# Last non-empty roster — preserved across empty fetches as a stale fallback.
_last_non_empty_cache: tuple[float, list[dict[str, Any]]] | None = None

# Retry configuration for roster fetches — transient network blips or
# upstream 5xx should not poison the cache for a full TTL.
_ROSTER_RETRY_CONFIG = RetryConfig(
    max_retries=2,
    backoff_base=1.0,
    backoff_cap=10.0,
    jitter_factor=0.5,
)


def _cache_valid(ttl: float) -> bool:
    """Check whether the cached roster is still fresh."""
    if _cache is None:
        return False
    fetched_at, _ = _cache
    return (time.monotonic() - fetched_at) < ttl


def _augment_with_fallbacks(
    entries: list[dict[str, Any]],
    fallbacks: dict[str, str],
) -> list[dict[str, Any]]:
    """Augment *entries* with any fallback components not already present.

    Returns a new list — the original *entries* is not mutated.
    Logged at INFO so operators can see which components are running on
    baked-in fallbacks rather than the live roster.
    """
    if not fallbacks:
        return entries
    existing_ids = {e.get("id") for e in entries if not e.get("_error")}
    augmented = list(entries)
    for cid, base_url in fallbacks.items():
        if cid in existing_ids:
            continue
        logger.info(
            "Using baked-in fallback for component '%s' at %s "
            "(not in central-deploy roster)",
            cid,
            base_url,
        )
        augmented.append(
            {
                "id": cid,
                "base_url": base_url,
                "skill": "",
                "_fallback": True,
            }
        )
    return augmented


async def fetch_roster(
    settings: CentralDeploySettings,
) -> list[dict[str, Any]]:
    """Fetch the component roster from central-deploy.

    Returns a cached result when still fresh; on a cache miss fetches
    ``GET {url}/chat/components`` with the bearer token.

    When *settings.component_fallbacks* is non-empty, any component ids
    not present in the fetched roster are added from the fallback map so
    that monitors and tool calls keep working through transient roster
    gaps (e.g. after a redeploy).

    Args:
        settings: Central-deploy configuration (url, ttl,
            component_fallbacks).

    Returns:
        A list of component entries (each with ``id``, ``base_url``,
        ``skill``). Returns an empty list when ``url`` is empty, and
        a list with a single error-entry when the fetch fails.

    """
    global _cache, _last_non_empty_cache
    if not settings.url:
        # Even without a central-deploy URL, still honour fallbacks so
        # standalone component access works.
        if settings.component_fallbacks:
            ids = ", ".join(
                f"{k} ({v})" for k, v in settings.component_fallbacks.items()
            )
            logger.info(
                "No central-deploy URL configured; using baked-in "
                "component fallbacks only: %s",
                ids,
            )
            return _augment_with_fallbacks([], settings.component_fallbacks)
        return []

    if _cache_valid(settings.roster_cache_ttl):
        _, entries = _cache  # type: ignore[misc]
        return _augment_with_fallbacks(entries, settings.component_fallbacks)

    roster_url = f"{settings.url.rstrip('/')}/chat/components"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            retry_client = RetryClient(client, config=_ROSTER_RETRY_CONFIG)
            resp = await retry_client.get(roster_url)
            resp.raise_for_status()
            entries = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch component roster: %s", exc)
        entries = [
            {
                "id": "_error",
                "base_url": "",
                "skill": "",
                "_error": f"Roster unavailable: {exc}",
            }
        ]

    if not isinstance(entries, list):
        logger.warning("Roster response is not a list: %r", type(entries))
        entries = []

    if not entries or (len(entries) == 1 and entries[0].get("_error")):
        logger.warning("Fetched component roster is empty or errored")
        # Do not cache an empty roster for the full TTL — a transient
        # upstream blip would lock out all component_request calls.
        # Fall back to the last non-empty roster if available, but
        # do NOT update _cache (empty must never poison the cache).
        if _last_non_empty_cache is not None:
            return _augment_with_fallbacks(
                _last_non_empty_cache[1], settings.component_fallbacks
            )
        # No cached fallback available — return empty/error augmented
        # with any baked-in fallbacks.
        return _augment_with_fallbacks(entries, settings.component_fallbacks)

    # Log the roster at startup / on first fetch so operators can
    # verify which components are registered.
    non_error = [e for e in entries if not e.get("_error")]
    if non_error:
        ids = ", ".join(f"{e['id']} ({e.get('base_url', '?')})" for e in non_error)
        logger.info("Component roster loaded: %s", ids)
    if settings.component_fallbacks:
        existing_ids = {e.get("id") for e in entries if not e.get("_error")}
        missing = {
            k: v
            for k, v in settings.component_fallbacks.items()
            if k not in existing_ids
        }
        if missing:
            logger.info(
                "Component fallbacks available for missing entries: %s",
                ", ".join(f"{k} ({v})" for k, v in missing.items()),
            )

    # Cache non-empty, non-error entries.
    _cache = (time.monotonic(), entries)
    _last_non_empty_cache = _cache
    return _augment_with_fallbacks(entries, settings.component_fallbacks)


def fetch_roster_sync(settings: CentralDeploySettings) -> list[dict[str, Any]]:
    """Fetch the component roster synchronously, for startup-time use.

    Used by ``create_agent_from_settings`` to prime the roster cache and
    build the initial skill prompt before the async event loop is running.

    Delegates to :func:`fetch_roster` via :func:`asyncio.run`.
    """
    import asyncio

    return asyncio.run(fetch_roster(settings))


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
