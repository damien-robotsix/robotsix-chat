"""Tests for the shared GitHub App token helper and the real library contract.

Two layers:

* **Shared helper** (:func:`github_app_token`) — pinned vs per-repo minting,
  ``None`` on failure/absent credentials, exercised with the faked
  ``robotsix_github_auth`` pattern used across the caller suites.
* **Real pinned library contract** — the empty-installation-id path relies on
  ``mint_installation_token`` accepting ``owner``/``repo`` (per-repo
  resolution, the recommended default).  These tests import the *real*
  ``robotsix-github-auth`` dependency and prove that contract directly, so a
  library revision dropping ``owner``/``repo`` cannot silently degrade to an
  unauthenticated fetch behind a green CI (the failure would surface here as
  a ``TypeError``-driven test failure instead of being swallowed).
"""

from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from robotsix_chat.common.github_app_token import github_app_token
from robotsix_chat.config import DirectRepoSettings


def _direct_repo(
    *,
    github_app_id: str = "12345",
    private_key: str = "fake-key",  # pragma: allowlist secret
    installation_id: str = "",
) -> DirectRepoSettings:
    """Minimal DirectRepoSettings; App credentials configured by default."""
    return DirectRepoSettings(
        github_app_id=github_app_id,
        github_app_private_key=private_key,
        github_app_installation_id=installation_id,
    )


# ---------------------------------------------------------------------------
# Shared helper — faked robotsix_github_auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_pins_installation_id_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned installation id is passed straight to mint_installation_token."""
    minted: list[tuple[Any, ...]] = []

    def _fake_mint(**kw: object) -> object:
        minted.append((kw.get("installation_id"), kw.get("owner"), kw.get("repo")))
        return SimpleNamespace(token="app-token")

    monkeypatch.setitem(
        sys.modules,
        "robotsix_github_auth",
        SimpleNamespace(mint_installation_token=_fake_mint),
    )

    token = await github_app_token(
        _direct_repo(installation_id="67890"), owner="org", repo="my-repo"
    )
    assert token == "app-token"
    assert minted == [("67890", None, None)]


@pytest.mark.asyncio
async def test_helper_resolves_per_repo_when_no_id_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty installation id: mint_installation_token gets owner/repo."""
    minted: list[tuple[Any, ...]] = []

    def _fake_mint(**kw: object) -> object:
        minted.append((kw.get("installation_id"), kw.get("owner"), kw.get("repo")))
        return SimpleNamespace(token="per-repo-token")

    monkeypatch.setitem(
        sys.modules,
        "robotsix_github_auth",
        SimpleNamespace(mint_installation_token=_fake_mint),
    )

    token = await github_app_token(_direct_repo(), owner="org", repo="my-repo")
    assert token == "per-repo-token"
    assert minted == [(None, "org", "my-repo")]


@pytest.mark.asyncio
async def test_helper_returns_none_on_mint_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minting failure becomes ``None`` — the helper never raises."""

    def _failing_mint(**kw: object) -> object:
        raise RuntimeError("no token")

    monkeypatch.setitem(
        sys.modules,
        "robotsix_github_auth",
        SimpleNamespace(mint_installation_token=_failing_mint),
    )

    token = await github_app_token(
        _direct_repo(installation_id="67890"), owner="org", repo="my-repo"
    )
    assert token is None


@pytest.mark.asyncio
async def test_helper_returns_none_when_not_configured() -> None:
    """No App credentials → ``None`` without touching the library."""
    token = await github_app_token(
        _direct_repo(github_app_id=""), owner="org", repo="my-repo"
    )
    assert token is None


# ---------------------------------------------------------------------------
# Real pinned library contract — owner/repo per-repo resolution
# ---------------------------------------------------------------------------


def test_pinned_library_mint_accepts_owner_and_repo() -> None:
    """The real mint_installation_token signature accepts owner/repo kwargs."""
    import robotsix_github_auth as real

    params = inspect.signature(real.mint_installation_token).parameters
    assert "owner" in params
    assert "repo" in params
    for name in ("owner", "repo"):
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY


def test_pinned_library_requires_owner_repo_when_no_installation_id() -> None:
    """Omitting both id and owner/repo raises the library's own error.

    ``TokenMintError`` (not a ``TypeError`` about unexpected kwargs) proves
    ``owner``/``repo`` are the intended public per-repo API.
    """
    import robotsix_github_auth as real
    from robotsix_github_auth import TokenMintError

    with pytest.raises(TokenMintError, match="owner and repo"):
        real.mint_installation_token(
            app_id="12345", private_key="fake-key"
        )  # pragma: allowlist secret


def test_pinned_library_accepts_owner_repo_kwargs() -> None:
    """Calling with owner/repo never raises TypeError — only TokenMintError.

    A bogus private key makes the local JWT signing fail with the library's
    own ``TokenMintError``; if the library did not accept ``owner``/``repo``
    this call would raise ``TypeError: unexpected keyword argument`` instead.
    """
    import robotsix_github_auth as real
    from robotsix_github_auth import TokenMintError

    with pytest.raises(TokenMintError):
        real.mint_installation_token(
            app_id="12345",
            private_key="not-a-real-key",  # pragma: allowlist secret
            owner="org",
            repo="my-repo",
        )


def test_pinned_library_resolves_installation_per_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real mint_installation_token resolves the installation per repo.

    The library's network-facing internals (JWT build, installation
    resolution, token mint) are stubbed so the real per-repo code path is
    exercised without network; this proves the recommended empty-id default
    routes ``owner``/``repo`` into installation resolution.
    """
    import robotsix_github_auth as real
    from robotsix_github_auth import _auth
    from robotsix_github_auth._models import InstallationToken

    monkeypatch.setattr(
        _auth, "_build_app_jwt", lambda app_id, private_key: "jwt-token"
    )

    resolved: list[str] = []
    if hasattr(_auth, "_resolve_installation_id_for_repo"):
        # Newer rev: the resolver is keyed on "owner/repo" and minting +
        # token caching live inside _mint_with_cache.
        def _resolver(jwt_token: str, repo_full_name: str) -> str:
            resolved.append(repo_full_name)
            return "999"

        def _mint(
            jwt_token: str, resolved_id: str, scopes: object = None
        ) -> InstallationToken:
            return InstallationToken(
                token="per-repo-token", expires_at=datetime.now(UTC)
            )

        monkeypatch.setattr(_auth, "_resolve_installation_id_for_repo", _resolver)
        monkeypatch.setattr(_auth, "_mint_with_cache", _mint)
    else:
        # Pinned rev: the resolver is keyed on (owner, repo) and minting is
        # _mint_token behind an explicit token cache + per-key lock.
        def _resolver(jwt_token: str, owner: str, repo: str) -> str:
            resolved.append(f"{owner}/{repo}")
            return "999"

        def _mint(
            jwt_token: str, resolved_id: str, scopes: object = None
        ) -> InstallationToken:
            return InstallationToken(
                token="per-repo-token", expires_at=datetime.now(UTC)
            )

        monkeypatch.setattr(_auth, "_resolve_installation_id", _resolver)
        monkeypatch.setattr(_auth, "_mint_token", _mint)
        monkeypatch.setattr(
            _auth,
            "_token_cache",
            SimpleNamespace(get=lambda *a: None, put=lambda *a: None),
        )
        monkeypatch.setattr(
            _auth,
            "_acquire_mint_lock",
            lambda key: SimpleNamespace(release=lambda: None),
        )

    token = real.mint_installation_token(
        app_id="12345",
        private_key="fake-key",  # pragma: allowlist secret
        owner="org",
        repo="my-repo",
    )

    assert resolved == ["org/my-repo"]
    assert token.token == "per-repo-token"
