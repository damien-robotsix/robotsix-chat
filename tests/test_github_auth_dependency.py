"""Regression guard for the robotsix-github-auth runtime dependency.

The chat mints GitHub App installation tokens via the shared
``robotsix_github_auth`` library (direct-repo, refdocs, version-check,
repo-study). It must be a *real* declared+locked dependency — not merely
mocked in tests — or the deployed container raises ``ModuleNotFoundError``
on the first token mint. This test imports the genuine module (no mock) so
a green CI can never again hide a missing dependency.
"""


def test_robotsix_github_auth_is_a_real_installed_dependency() -> None:
    import robotsix_github_auth  # noqa: F401
    from robotsix_github_auth import mint_installation_token

    assert callable(mint_installation_token)
