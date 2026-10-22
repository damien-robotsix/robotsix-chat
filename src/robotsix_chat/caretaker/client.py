"""HTTP client for central-deploy's ``GET /caretaker/status`` endpoint."""

from __future__ import annotations

import logging

import httpx

from robotsix_chat.caretaker.models import CaretakerFinding, parse_findings
from robotsix_chat.config.deploy_models import CentralDeploySettings

logger = logging.getLogger(__name__)


async def fetch_caretaker_status(
    central_deploy: CentralDeploySettings,
    *,
    timeout: float,
) -> list[CaretakerFinding]:
    """Fetch and parse central-deploy caretaker findings.

    Issues ``GET {central_deploy.url}/caretaker/status`` with the shared
    ``X-API-Key`` deploy credential (when configured) and parses the
    response into :class:`CaretakerFinding` records.

    The URL is built via :meth:`httpx.URL.copy_with` so the operator-set
    base URL cannot smuggle a different host/path into the request.

    Raises the underlying :class:`httpx.HTTPError` on transport or status
    errors — the caller (the watcher loop) logs and retries on the next
    poll.  A malformed-but-200 body degrades to an empty finding list
    rather than raising.
    """
    base = httpx.URL(central_deploy.url.rstrip("/"))
    status_url = base.copy_with(path="/caretaker/status")

    headers: dict[str, str] = {}
    api_key = central_deploy.deploy_api_key.get_secret_value()
    if api_key:
        headers["X-API-Key"] = api_key

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        response = await client.get(str(status_url), headers=headers)
        response.raise_for_status()
        payload = response.json()

    return parse_findings(payload)
