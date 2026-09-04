"""GitHub App authentication header helper.

Consolidates the GitHub App token-minting logic that was duplicated
across the refdocs, version_check, direct client, and repo-study HTTP clients.
"""

from __future__ import annotations

import asyncio
import logging

from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)


async def _build_github_app_auth_headers(
    dr: DirectRepoSettings,
    label: str,
    token_cache: dict[str, str] | None = None,
) -> str | None:
    """Mint a GitHub App installation token and return the raw token string.

    When *token_cache* is provided, results are cached by installation ID
    (the dict is mutated in-place).  Returns ``None`` when credentials are
    missing or token minting fails, so callers can fall back to
    unauthenticated requests or raise their own error.
    """
    if not (
        dr.github_app_id
        and dr.github_app_private_key.get_secret_value()
        and dr.github_app_installation_id
    ):
        return None

    if token_cache is not None:
        cached = token_cache.get(dr.github_app_installation_id)
        if cached is not None:
            return cached

    from robotsix_github_auth import mint_installation_token

    try:
        result = await asyncio.to_thread(
            mint_installation_token,
            app_id=dr.github_app_id,
            private_key=dr.github_app_private_key.get_secret_value(),
            installation_id=dr.github_app_installation_id,
        )
        token = result.token
        if token_cache is not None:
            token_cache[dr.github_app_installation_id] = token
        return token
    except RuntimeError as exc:
        logger.warning(
            "%s GitHub App token unavailable, "
            "falling back to unauthenticated fetch: %s",
            label,
            exc,
        )
        return None
