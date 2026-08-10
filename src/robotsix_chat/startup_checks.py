"""Startup connectivity checks for the component roster.

At container startup, iterates over the component roster and performs a
``GET <base_url>/health`` for each entry.  Unreachable components are
logged at ``WARNING`` so stale roster entries surface immediately instead
of during an incident.  The check is **non-fatal** — startup continues
even when every component is unreachable.
"""

from __future__ import annotations

import logging

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.component_access.roster import fetch_roster
from robotsix_chat.config import CentralDeploySettings

logger = logging.getLogger(__name__)


async def check_component_connectivity(settings: CentralDeploySettings) -> None:
    """Check ``GET /health`` reachability for every rostered component.

    Fetches the roster from central-deploy (or its cache), then probes
    ``<base_url>/health`` for each component entry.  Warnings are logged
    per unreachable component; no exception escapes — the check is
    advisory only.

    Args:
        settings: Central-deploy configuration for roster fetch.

    """
    try:
        entries = await fetch_roster(settings)
    except Exception:
        logger.warning(
            "Startup connectivity check skipped — roster fetch failed",
            exc_info=True,
        )
        return

    valid = [e for e in entries if not e.get("_error") and e.get("base_url")]
    if not valid:
        logger.info(
            "Startup connectivity check: no components to probe "
            "(roster is empty or all entries are errored)"
        )
        return

    for entry in valid:
        cid = entry.get("id", "?")
        base_url = entry["base_url"].rstrip("/")
        health_url = f"{base_url}/health"

        try:
            result = await safe_http_request(
                "GET", health_url, timeout=10.0, label=f"Component '{cid}'"
            )
        except Exception:
            logger.warning(
                "Component '%s' health check raised an unexpected exception: %s",
                cid,
                health_url,
                exc_info=True,
            )
            continue

        if result.ok:
            logger.debug("Component '%s' health check OK (%s)", cid, health_url)
        else:
            logger.warning(
                "Component '%s' is unreachable at %s: %s",
                cid,
                health_url,
                result.error,
            )
