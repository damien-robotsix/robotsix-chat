"""Regression guard for the robotsix-github-auth runtime dependency.

The chat mints GitHub App installation tokens via the shared
``robotsix_github_auth`` library (direct-repo, refdocs, version-check,
repo-study). It must be a *real* declared+locked dependency — not merely
mocked in tests — or the deployed container raises ``ModuleNotFoundError``
on the first token mint. This test imports the genuine module (no mock) so
a green CI can never again hide a missing dependency.

The direct-repo client's 401-refresh path (``_invalidate_token``) also
delegates to the library's public ``clear_token_cache``; that symbol is
asserted against the real module here as well, since every other test
module fakes it onto a mock.
"""


def test_robotsix_github_auth_is_a_real_installed_dependency() -> None:
    """Verify the shared library imports and exposes ``mint_installation_token``."""
    import robotsix_github_auth  # noqa: F401
    from robotsix_github_auth import mint_installation_token

    assert callable(mint_installation_token)


def test_pinned_library_exposes_clear_token_cache() -> None:
    """The real library exposes the 401-refresh invalidation API.

    ``_invalidate_token`` calls ``clear_token_cache`` on a 401 inside
    ``_http_with_retry``; every test module mocks it onto a fake, so a
    library revision that renames or drops the symbol would raise
    ``ImportError`` in production behind a green CI.  Importing the genuine
    module and calling the symbol with no required args proves the public
    contract directly.
    """
    import robotsix_github_auth as real
    from robotsix_github_auth import clear_token_cache

    assert callable(clear_token_cache)
    # A no-arg call must succeed against the real module (plain cache clear).
    assert clear_token_cache() is None
    assert hasattr(real, "clear_token_cache")
