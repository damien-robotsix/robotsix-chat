"""Tests for :class:`PROperationsMixin` (``client_pr_operations.py``).

These exercise the multi-step PR methods (``create_pr``, ``update_pr_branch``
and ``resolve_pr_conflict``) directly on a :class:`DirectRepoClient` instance,
stubbing the HTTP helper methods (``_get_json`` / ``_post_json`` /
``_patch_json`` / ``_http_with_retry`` / ``_gh_headers`` / ``_git_create_tree``)
so no real network calls happen.  Shared fixtures live in
``tests/repo/direct/conftest.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robotsix_chat.repo.direct.client import DirectRepoClient

from .conftest import _settings

# ============================================================================
# create_pr — default-branch fetch / fallback
# ============================================================================


@pytest.mark.asyncio
async def test_create_pr_uses_repo_default_branch() -> None:
    """The base branch is taken from the repo's ``default_branch`` field."""
    client = DirectRepoClient(_settings())
    posted: dict[str, Any] = {}

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        assert path == "/repos/org/repo"
        return {"default_branch": "develop"}

    async def _fake_post_json(path: str, body: dict[str, Any]) -> Any:
        posted["path"] = path
        posted["body"] = body
        return {"html_url": "https://github.com/org/repo/pull/7"}

    client._get_json = _fake_get_json  # type: ignore[method-assign]
    client._post_json = _fake_post_json  # type: ignore[method-assign]

    out = await client.create_pr(
        repo_full_name="org/repo",
        head_branch="feature",
        title="Add feature",
        body="Body",
    )

    assert posted["path"] == "/repos/org/repo/pulls"
    assert posted["body"]["base"] == "develop"
    assert posted["body"]["head"] == "feature"
    assert "opened successfully" in out
    assert "https://github.com/org/repo/pull/7" in out
    assert "human review required" in out


@pytest.mark.asyncio
async def test_create_pr_falls_back_to_main() -> None:
    """When the repo object omits ``default_branch``, base falls back to ``main``."""
    client = DirectRepoClient(_settings())
    posted: dict[str, Any] = {}

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return {}  # no default_branch key

    async def _fake_post_json(path: str, body: dict[str, Any]) -> Any:
        posted["body"] = body
        return {"html_url": "https://github.com/org/repo/pull/8"}

    client._get_json = _fake_get_json  # type: ignore[method-assign]
    client._post_json = _fake_post_json  # type: ignore[method-assign]

    out = await client.create_pr(
        repo_full_name="org/repo",
        head_branch="feature",
        title="t",
        body="b",
    )

    assert posted["body"]["base"] == "main"
    assert "opened successfully" in out


@pytest.mark.asyncio
async def test_create_pr_error_is_formatted() -> None:
    """A failure in the create call is caught and returned as an error string."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return {"default_branch": "main"}

    async def _fake_post_json(path: str, body: dict[str, Any]) -> Any:
        raise RuntimeError("GitHub API POST failed: 403")

    client._get_json = _fake_get_json  # type: ignore[method-assign]
    client._post_json = _fake_post_json  # type: ignore[method-assign]

    out = await client.create_pr(
        repo_full_name="org/repo",
        head_branch="feature",
        title="t",
        body="b",
    )

    assert out.startswith("Error opening PR:")
    assert "403" in out


# ============================================================================
# update_pr_branch — success / 422 conflict / other error / exception
# ============================================================================


def _stub_update_branch(client: DirectRepoClient, result: Any) -> None:
    """Stub ``_gh_headers`` and ``_http_with_retry`` to return *result*."""

    async def _fake_headers(**_kw: Any) -> dict[str, str]:
        return {"Authorization": "token x"}

    async def _fake_http(method: str, url: str, **_kw: Any) -> Any:
        return result

    client._gh_headers = _fake_headers  # type: ignore[method-assign]
    client._http_with_retry = _fake_http  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_update_pr_branch_success() -> None:
    """An ``ok`` result yields the queued-for-update message."""
    client = DirectRepoClient(_settings())
    _stub_update_branch(client, SimpleNamespace(ok=True, status_code=202, error=None))

    out = await client.update_pr_branch(repo_full_name="org/repo", pr_number=42)

    assert "42" in out
    assert "branch update" in out


@pytest.mark.asyncio
async def test_update_pr_branch_conflict_422() -> None:
    """A 422 result is reported as a merge conflict, echoing the detail."""
    client = DirectRepoClient(_settings())
    _stub_update_branch(
        client,
        SimpleNamespace(ok=False, status_code=422, error="not mergeable"),
    )

    out = await client.update_pr_branch(repo_full_name="org/repo", pr_number=99)

    assert "merge conflict" in out.lower()
    assert "not mergeable" in out
    assert "99" in out


@pytest.mark.asyncio
async def test_update_pr_branch_other_error() -> None:
    """A non-422 failure is reported through the generic error branch."""
    client = DirectRepoClient(_settings())
    _stub_update_branch(
        client,
        SimpleNamespace(ok=False, status_code=500, error="server exploded"),
    )

    out = await client.update_pr_branch(repo_full_name="org/repo", pr_number=1)

    assert out.startswith("Error updating PR branch:")
    assert "server exploded" in out
    assert "merge conflict" not in out.lower()


@pytest.mark.asyncio
async def test_update_pr_branch_exception_is_caught() -> None:
    """An exception raised while building the request is caught and formatted."""
    client = DirectRepoClient(_settings())

    async def _boom(**_kw: Any) -> dict[str, str]:
        raise RuntimeError("token mint failed")

    client._gh_headers = _boom  # type: ignore[method-assign]

    out = await client.update_pr_branch(repo_full_name="org/repo", pr_number=1)

    assert out.startswith("Error updating PR branch:")
    assert "token mint failed" in out


# ============================================================================
# resolve_pr_conflict — state validation & SHA resolution branches
# ============================================================================


def _pr(
    *,
    state: str = "open",
    mergeable: Any = False,
    head_ref: str | None = "feature",
    head_repo_full_name: str | None = "org/repo",
    base_ref: str | None = "main",
) -> dict[str, Any]:
    """Build a minimal PR object for ``resolve_pr_conflict``."""
    head: dict[str, Any] = {}
    if head_ref is not None:
        head["ref"] = head_ref
    if head_repo_full_name is not None:
        head["repo"] = {"full_name": head_repo_full_name}
    base: dict[str, Any] = {}
    if base_ref is not None:
        base["ref"] = base_ref
    return {
        "state": state,
        "mergeable": mergeable,
        "mergeable_state": "dirty",
        "head": head,
        "base": base,
    }


@pytest.mark.asyncio
async def test_resolve_pr_conflict_get_pr_failure() -> None:
    """A RuntimeError fetching the PR is caught and returned."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        raise RuntimeError("404 not found")

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert out.startswith("Error resolving conflict on PR #5")
    assert "404 not found" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_not_open() -> None:
    """A closed PR short-circuits with a state message."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return _pr(state="closed")

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert "is closed, not open" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_already_mergeable() -> None:
    """``mergeable is True`` means there is nothing to resolve."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return _pr(mergeable=True)

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert "no merge conflict" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_mergeability_unknown() -> None:
    """``mergeable is None`` (still computing) refuses to act."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return _pr(mergeable=None)

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert "still being computed" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_missing_head_branch() -> None:
    """A PR with no head ref cannot locate where to commit."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return _pr(head_ref=None)

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert "has no head" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_cross_repo_refused() -> None:
    """A fork head repo is refused (cross-repo resolution is not permitted)."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return _pr(head_repo_full_name="fork/repo")

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert "Cross-repo" in out
    assert "fork/repo" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_missing_base_branch() -> None:
    """A PR with no base ref cannot determine the second merge parent."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        return _pr(base_ref=None)

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert "has no base" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_sha_resolution_error() -> None:
    """A malformed ref object (no ``object.sha``) is caught as a SHA error."""
    client = DirectRepoClient(_settings())

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        if "/git/ref/heads/" in path:
            return {}  # missing "object" → KeyError
        return _pr()

    client._get_json = _fake_get_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[],
        commit_message="m",
    )
    assert "could not read head/base branch SHAs" in out


@pytest.mark.asyncio
async def test_resolve_pr_conflict_happy_path() -> None:
    """Full flow: overlay tree → two-parent merge commit → fast-forward ref."""
    client = DirectRepoClient(_settings())
    pulls_calls = {"n": 0}
    patched: dict[str, Any] = {}
    committed: dict[str, Any] = {}

    async def _fake_get_json(path: str, **_kw: Any) -> Any:
        if "/pulls/" in path and "/git/" not in path:
            pulls_calls["n"] += 1
            if pulls_calls["n"] == 1:
                return _pr()  # conflicting
            # Re-check after ref update → now clean.
            return _pr(mergeable=True) | {"mergeable_state": "clean"}
        if path.endswith("/git/ref/heads/feature"):
            return {"object": {"sha": "headsha0000000"}}
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "basesha0000000"}}
        if "/git/commits/" in path:
            return {"tree": {"sha": "headtreesha"}}
        raise AssertionError(f"unexpected GET {path}")

    async def _fake_git_create_tree(
        repo_full_name: str, base_tree_sha: str, files: list[dict[str, str]]
    ) -> str:
        assert base_tree_sha == "headtreesha"
        return "mergedtreesha"

    async def _fake_post_json(path: str, body: dict[str, Any]) -> Any:
        committed["path"] = path
        committed["body"] = body
        return {"sha": "mergecommitsha"}

    async def _fake_patch_json(path: str, body: dict[str, Any]) -> Any:
        patched["path"] = path
        patched["body"] = body
        return {}

    client._get_json = _fake_get_json  # type: ignore[method-assign]
    client._git_create_tree = _fake_git_create_tree  # type: ignore[method-assign]
    client._post_json = _fake_post_json  # type: ignore[method-assign]
    client._patch_json = _fake_patch_json  # type: ignore[method-assign]

    out = await client.resolve_pr_conflict(
        repo_full_name="org/repo",
        pr_number=5,
        resolved_files=[{"path": "a.py", "content": "x"}],
        commit_message="resolve it",
    )

    # Merge commit carries both parents in the documented order.
    assert committed["path"] == "/repos/org/repo/git/commits"
    assert committed["body"]["tree"] == "mergedtreesha"
    assert committed["body"]["parents"] == ["headsha0000000", "basesha0000000"]
    assert committed["body"]["message"] == "resolve it"
    # Head ref fast-forwarded to the merge commit, no force.
    assert patched["path"] == "/repos/org/repo/git/refs/heads/feature"
    assert patched["body"] == {"sha": "mergecommitsha", "force": False}
    # Re-check reflects the now-clean state.
    assert "resolved" in out
    assert "mergecommitsha" in out
    assert "clean" in out
