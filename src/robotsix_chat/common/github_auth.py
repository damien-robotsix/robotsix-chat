"""GitHub App authentication header helper.

Consolidates the GitHub App token-minting logic that was duplicated
across the refdocs, version_check, direct client, and repo-study HTTP clients.
"""

from __future__ import annotations

import asyncio
import logging

from robotsix_chat.config import DirectRepoSettings

logger = logging.getLogger(__name__)

# Per-repo installation resolution cache: ``"owner/repo"`` -> installation id.
# Mirrors ``repo/direct/client.py``'s cache so a resolution made by refdocs,
# version_check, or repo_study is reused across calls for the same repo.
_RESOLVED_INSTALLATION_CACHE: dict[str, str] = {}


def _resolve_installation_id_for_repo(
    app_id: str, private_key: str, owner: str, repo: str
) -> str:
    """Resolve (and cache) the installation id GitHub uses for ``owner/repo``.

    Mirrors robotsix-github-auth's ``_resolve_installation_id`` (a
    ``GET /repos/{owner}/{repo}/installation`` with the App JWT) and the
    same-named helper in ``repo/direct/client.py``.  Runs synchronously
    (blocking httpx); call it via ``asyncio.to_thread``.
    """
    key = f"{owner}/{repo}"
    cached = _RESOLVED_INSTALLATION_CACHE.get(key)
    if cached is not None:
        return cached
    from robotsix_github_auth._auth import (
        _build_app_jwt,
        _resolve_installation_id,
    )

    jwt_token = _build_app_jwt(app_id, private_key)
    resolved = str(_resolve_installation_id(jwt_token, owner, repo))
    _RESOLVED_INSTALLATION_CACHE[key] = resolved
    return resolved


async def _build_github_app_auth_headers(
    dr: DirectRepoSettings,
    label: str,
    token_cache: dict[str, str] | None = None,
    *,
    owner: str | None = None,
    repo: str | None = None,
) -> str | None:
    """Mint a GitHub App installation token and return the raw token string.

    When no ``github_app_installation_id`` is pinned (the recommended
    default) and *owner*/*repo* are provided, the installation is resolved
    per repository via ``GET /repos/{owner}/{repo}/installation`` and
    cached — mirroring the direct-repo client — so private repos stay
    authenticated across App re-installs without a config edit.  When an id
    is pinned it is used as-is.

    When *token_cache* is provided, results are cached by installation ID
    (the dict is mutated in-place).  Returns ``None`` when credentials are
    missing, no repository context is available to resolve against, or
    token minting fails, so callers can fall back to unauthenticated
    requests or raise their own error.
    """
    if not (dr.github_app_id and dr.github_app_private_key.get_secret_value()):
        return None

    installation_id = dr.github_app_installation_id
    if not installation_id:
        if not (owner and repo):
            return None
        try:
            installation_id = await asyncio.to_thread(
                _resolve_installation_id_for_repo,
                dr.github_app_id,
                dr.github_app_private_key.get_secret_value(),
                owner,
                repo,
            )
        except Exception as exc:
            logger.warning(
                "%s GitHub App installation resolution failed, "
                "falling back to unauthenticated fetch: %s",
                label,
                exc,
            )
            return None

    if token_cache is not None:
        cached = token_cache.get(installation_id)
        if cached is not None:
            return cached

    from robotsix_github_auth import mint_installation_token

    try:
        result = await asyncio.to_thread(
            mint_installation_token,
            app_id=dr.github_app_id,
            private_key=dr.github_app_private_key.get_secret_value(),
            installation_id=installation_id,
        )
        token = result.token
        if token_cache is not None:
            token_cache[installation_id] = token
        return token
    except RuntimeError as exc:
        logger.warning(
            "%s GitHub App token unavailable, "
            "falling back to unauthenticated fetch: %s",
            label,
            exc,
        )
        return None
