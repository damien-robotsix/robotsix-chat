"""Shared GitHub App installation-token helper.

Consolidates the per-caller ``_github_app_token`` adapters that used to be
triplicated across the refdocs, version-check, and repo-study clients onto
one thin adapter over robotsix-github-auth's public
``mint_installation_token``.  The heavy minting/caching logic lives in the
library; this helper only pins the configured installation id (or passes
``owner``/``repo`` for per-repo resolution) and converts any failure into
``None`` so the caller can decide how to proceed.
"""

from __future__ import annotations

import asyncio
import logging

from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)


async def github_app_token(
    dr: DirectRepoSettings,
    *,
    owner: str,
    repo: str,
) -> str | None:
    """Mint a GitHub App installation token, or ``None`` on any failure.

    Delegates to robotsix-github-auth's public ``mint_installation_token``,
    which resolves the installation per repository via ``owner``/``repo``
    when no id is pinned (the recommended default) and caches tokens
    internally.  When an installation id *is* pinned it is used directly,
    preserving the previous local behaviour.

    Never raises: when credentials are missing, or minting fails for any
    reason (including the library rejecting a misconfiguration), ``None``
    is returned so the caller can fall back to an unauthenticated request
    or surface the failure itself — repo-study, for instance, raises a
    ``WorkspaceError`` when a pinned id was configured but the exchange
    failed.  The broad ``except Exception`` is deliberate: every caller
    prefers returning a user-facing string over propagating an exception
    into the agent loop, so a minting failure must not be able to leak out.
    """
    if not (dr.github_app_id and dr.github_app_private_key.get_secret_value()):
        return None
    from robotsix_github_auth import mint_installation_token

    try:
        if dr.github_app_installation_id:
            result = await asyncio.to_thread(
                mint_installation_token,
                app_id=dr.github_app_id,
                private_key=dr.github_app_private_key.get_secret_value(),
                installation_id=dr.github_app_installation_id,
            )
        else:
            result = await asyncio.to_thread(
                mint_installation_token,
                app_id=dr.github_app_id,
                private_key=dr.github_app_private_key.get_secret_value(),
                owner=owner,
                repo=repo,
            )
    except Exception as exc:
        logger.warning(
            "GitHub App token unavailable for %s/%s, falling back to "
            "unauthenticated fetch: %s",
            owner,
            repo,
            exc,
        )
        return None
    return result.token
