"""GitHub App authentication header helper.

Consolidates the GitHub App token-minting logic that was duplicated
between the refdocs and version_check HTTP clients.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)


async def _build_github_app_auth_headers(
    dr: DirectRepoSettings,
    label: str,
) -> str | None:
    """Mint a GitHub App installation token and return the raw token string.

    Returns ``None`` when credentials are missing or token minting fails,
    so callers fall back to unauthenticated requests.
    """
    if (
        dr.github_app_id
        and dr.github_app_private_key.get_secret_value()
        and dr.github_app_installation_id
    ):
        from robotsix_github_auth import mint_installation_token

        try:
            result = await asyncio.to_thread(
                mint_installation_token,
                app_id=dr.github_app_id,
                private_key=dr.github_app_private_key.get_secret_value(),
                installation_id=dr.github_app_installation_id,
            )
            return cast(str, result.token)
        except RuntimeError as exc:
            logger.warning(
                "%s GitHub App token unavailable, "
                "falling back to unauthenticated fetch: %s",
                label,
                exc,
            )
    return None
