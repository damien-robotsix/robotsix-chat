"""Tests for the repo-study tools (tarball fetch + local read-only study)."""

from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

import pytest
import respx
from httpx import Response

from robotsix_chat.config import DirectRepoSettings, RepoStudySettings
from robotsix_chat.repo.study import build_repo_study_tools
from robotsix_chat.repo.study.workspace import (
    WorkspaceError,
    WorkspaceManager,
    _workspace_id,
)

TARBALL_URL = "https://api.github.com/repos/acme/widget/tarball"


def make_tarball(files: dict[str, bytes], prefix: str = "acme-widget-abc123") -> bytes:
    """Build an in-memory gzipped tarball shaped like a GitHub archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=f"{prefix}/{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> RepoStudySettings:
    """Build enabled repo-study settings rooted in a temp directory."""
    return RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws"))


@pytest.fixture
def manager(settings: RepoStudySettings) -> WorkspaceManager:
    """Build a manager with no GitHub App credentials (unauthenticated)."""
    return WorkspaceManager(settings, DirectRepoSettings())


SAMPLE_FILES = {
    "README.md": b"# Widget\n\nA sample repo.\n",
    "src/widget/core.py": b"def frobnicate():\n    return 42\n",
    "src/widget/util.py": b"VALUE = 'frobnicate me'\n",
    "assets/blob.bin": b"\x00\x01\x02binary",
}


@respx.mock
@pytest.mark.asyncio
async def test_fetch_extracts_and_summarizes(manager: WorkspaceManager) -> None:
    """Fetch downloads, extracts, and summarizes the workspace."""
    respx.get(TARBALL_URL).mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    summary = await manager.fetch("acme/widget")
    assert "acme--widget--default" in summary
    assert "4 files" in summary
    assert "README.md" in summary and "src/" in summary


@respx.mock
@pytest.mark.asyncio
async def test_fetch_with_ref_hits_ref_url(manager: WorkspaceManager) -> None:
    """A ref routes to the /tarball/<ref> URL and names the workspace."""
    route = respx.get(f"{TARBALL_URL}/v1.2").mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    summary = await manager.fetch("acme/widget", "v1.2")
    assert route.called
    assert "acme--widget--v1.2" in summary


@pytest.mark.asyncio
async def test_fetch_rejects_bad_repo_name(manager: WorkspaceManager) -> None:
    """A malformed repo name is rejected before any HTTP call."""
    with pytest.raises(WorkspaceError, match="owner/name"):
        await manager.fetch("not a repo!")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_surfaces_http_error(manager: WorkspaceManager) -> None:
    """An HTTP error status becomes a relayable WorkspaceError."""
    respx.get(TARBALL_URL).mock(return_value=Response(404))
    with pytest.raises(WorkspaceError, match="404"):
        await manager.fetch("acme/widget")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_enforces_archive_size_cap(tmp_path: Path) -> None:
    """Downloads larger than max_archive_bytes are aborted."""
    small = RepoStudySettings(
        enabled=True, data_dir=str(tmp_path / "ws"), max_archive_bytes=10
    )
    manager = WorkspaceManager(small, DirectRepoSettings())
    respx.get(TARBALL_URL).mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    with pytest.raises(WorkspaceError, match="byte limit"):
        await manager.fetch("acme/widget")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_enforces_extracted_size_cap(tmp_path: Path) -> None:
    """Archives expanding past max_extracted_bytes are aborted and cleaned."""
    small = RepoStudySettings(
        enabled=True, data_dir=str(tmp_path / "ws"), max_extracted_bytes=5
    )
    manager = WorkspaceManager(small, DirectRepoSettings())
    respx.get(TARBALL_URL).mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    with pytest.raises(WorkspaceError, match="Extracted size"):
        await manager.fetch("acme/widget")
    assert not (Path(small.data_dir) / "acme--widget--default").exists()


@respx.mock
@pytest.mark.asyncio
async def test_fetch_blocks_path_traversal_members(manager: WorkspaceManager) -> None:
    """Tar members escaping the workspace are rejected by the data filter."""
    evil = make_tarball({"../../evil.txt": b"pwned"})
    respx.get(TARBALL_URL).mock(return_value=Response(200, content=evil))
    with pytest.raises(WorkspaceError, match="Extraction failed"):
        await manager.fetch("acme/widget")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_sends_app_token_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured GitHub App creds put an installation token on the request."""
    direct_repo = DirectRepoSettings(
        github_app_id="42",
        github_app_private_key="fake-pem",  # pragma: allowlist secret
        github_app_installation_id="7",
    )

    def fake_token(**kw: object) -> object:
        return SimpleNamespace(token="installation-token")

    import sys
    from types import SimpleNamespace

    fake = SimpleNamespace()
    fake.mint_installation_token = fake_token
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)
    manager = WorkspaceManager(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")), direct_repo
    )
    route = respx.get(TARBALL_URL).mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    await manager.fetch("acme/widget")
    auth = route.calls.last.request.headers.get("Authorization")
    assert auth == "Bearer installation-token"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_raises_on_token_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed token exchange raises a WorkspaceError — no silent fallback."""
    direct_repo = DirectRepoSettings(
        github_app_id="42",
        github_app_private_key="fake-pem",  # pragma: allowlist secret
        github_app_installation_id="7",
    )

    def failing_token(**kw: object) -> object:
        raise RuntimeError("no token for you")

    import sys
    from types import SimpleNamespace

    fake = SimpleNamespace()
    fake.mint_installation_token = failing_token
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)
    manager = WorkspaceManager(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")), direct_repo
    )
    respx.get(TARBALL_URL).mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    with pytest.raises(WorkspaceError, match="token request failed"):
        await manager.fetch("acme/widget")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_403_reports_scope_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authenticated 403 reports missing ``contents:read`` permission."""
    direct_repo = DirectRepoSettings(
        github_app_id="42",
        github_app_private_key="fake-pem",  # pragma: allowlist secret
        github_app_installation_id="7",
    )

    import sys
    from types import SimpleNamespace

    def fake_token(**kw: object) -> object:
        return SimpleNamespace(token="installation-token")

    fake = SimpleNamespace()
    fake.mint_installation_token = fake_token
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)
    manager = WorkspaceManager(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")), direct_repo
    )
    respx.get(TARBALL_URL).mock(return_value=Response(403))
    with pytest.raises(WorkspaceError, match="contents:read"):
        await manager.fetch("acme/widget")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_404_unauthenticated_hints_private_repo(
    tmp_path: Path,
) -> None:
    """Unauthenticated 404 hints that the repo may be private."""
    manager = WorkspaceManager(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")),
        DirectRepoSettings(),  # no credentials
    )
    respx.get(TARBALL_URL).mock(return_value=Response(404))
    with pytest.raises(WorkspaceError, match="private"):
        await manager.fetch("acme/widget")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_404_authenticated_reports_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authenticated 404 reports repo not found / installation access issue."""
    direct_repo = DirectRepoSettings(
        github_app_id="42",
        github_app_private_key="fake-pem",  # pragma: allowlist secret
        github_app_installation_id="7",
    )

    import sys
    from types import SimpleNamespace

    def fake_token(**kw: object) -> object:
        return SimpleNamespace(token="installation-token")

    fake = SimpleNamespace()
    fake.mint_installation_token = fake_token
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)
    manager = WorkspaceManager(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")), direct_repo
    )
    respx.get(TARBALL_URL).mock(return_value=Response(404))
    with pytest.raises(WorkspaceError, match="authenticated"):
        await manager.fetch("acme/widget")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_follows_redirect_preserving_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 302 redirect to codeload is followed with the auth header intact."""
    direct_repo = DirectRepoSettings(
        github_app_id="42",
        github_app_private_key="fake-pem",  # pragma: allowlist secret
        github_app_installation_id="7",
    )

    import sys
    from types import SimpleNamespace

    def fake_token(**kw: object) -> object:
        return SimpleNamespace(token="installation-token")

    fake = SimpleNamespace()
    fake.mint_installation_token = fake_token
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)
    manager = WorkspaceManager(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")), direct_repo
    )
    codeload_url = "https://codeload.github.com/acme/widget/legacy.tar.gz/ref"
    api_route = respx.get(TARBALL_URL).mock(
        return_value=Response(302, headers={"Location": codeload_url})
    )
    codeload_route = respx.get(codeload_url).mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    summary = await manager.fetch("acme/widget")
    assert api_route.called
    assert codeload_route.called
    # The codeload request must carry the auth header that httpx would strip.
    codeload_auth = codeload_route.calls.last.request.headers.get("Authorization")
    assert codeload_auth == "Bearer installation-token"
    assert "acme--widget--default" in summary


# ---------------------------------------------------------------------------
# Study operations on a pre-fetched workspace
# ---------------------------------------------------------------------------


@pytest.fixture
def fetched(manager: WorkspaceManager) -> str:
    """Materialize a workspace on disk without HTTP; return its id."""
    ws_id = _workspace_id("acme/widget", "")
    root = Path(manager._s.data_dir) / ws_id
    for name, content in SAMPLE_FILES.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    meta = Path(manager._s.data_dir) / f"{ws_id}.meta.json"
    meta.write_text(json.dumps({"fetched_at": time.time()}))
    return ws_id


def test_list_files_with_glob(manager: WorkspaceManager, fetched: str) -> None:
    """list_files honours the glob filter."""
    listing = manager.list_files(fetched, "src/**/*.py")
    assert "src/widget/core.py" in listing
    assert "README.md" not in listing


def test_list_files_truncates(manager: WorkspaceManager, fetched: str) -> None:
    """list_files truncates at max_entries with a notice."""
    listing = manager.list_files(fetched, "**/*", max_entries=1)
    assert "truncated" in listing


def test_list_files_no_match(manager: WorkspaceManager, fetched: str) -> None:
    """list_files reports when nothing matches."""
    assert "No files match" in manager.list_files(fetched, "*.nope")


def test_read_file_numbers_lines(manager: WorkspaceManager, fetched: str) -> None:
    """read_file returns line-numbered content."""
    out = manager.read_file(fetched, "src/widget/core.py")
    assert out.splitlines()[0] == "1\tdef frobnicate():"


def test_read_file_window_and_truncation(
    manager: WorkspaceManager, fetched: str
) -> None:
    """read_file honours start_line/max_lines and flags truncation."""
    out = manager.read_file(fetched, "README.md", start_line=2, max_lines=1)
    assert out.startswith("2\t")
    assert "truncated" in out


def test_read_file_missing(manager: WorkspaceManager, fetched: str) -> None:
    """read_file errors clearly on a missing path."""
    with pytest.raises(WorkspaceError, match="No file"):
        manager.read_file(fetched, "nope.txt")


def test_read_file_rejects_traversal(manager: WorkspaceManager, fetched: str) -> None:
    """read_file rejects paths escaping the workspace."""
    with pytest.raises(WorkspaceError, match="escapes"):
        manager.read_file(fetched, "../../../etc/passwd")


def test_search_finds_matches_and_skips_binary(
    manager: WorkspaceManager, fetched: str
) -> None:
    """Search returns path:line matches and skips binary files."""
    out = manager.search(fetched, "frobnicate")
    assert "src/widget/core.py:1:" in out
    assert "src/widget/util.py:1:" in out
    assert "blob.bin" not in out


def test_search_truncates(manager: WorkspaceManager, fetched: str) -> None:
    """Search truncates at max_matches with a notice."""
    out = manager.search(fetched, "frobnicate", max_matches=1)
    assert "truncated" in out


def test_search_invalid_regex(manager: WorkspaceManager, fetched: str) -> None:
    """Search rejects an invalid regex with a clear error."""
    with pytest.raises(WorkspaceError, match="Invalid regex"):
        manager.search(fetched, "(")


def test_search_no_match(manager: WorkspaceManager, fetched: str) -> None:
    """Search reports when nothing matches."""
    assert "No matches" in manager.search(fetched, "zzz-never-there")


def test_drop_deletes_workspace(manager: WorkspaceManager, fetched: str) -> None:
    """Drop removes the workspace immediately."""
    assert "deleted" in manager.drop(fetched)
    with pytest.raises(WorkspaceError, match="Unknown workspace"):
        manager.list_files(fetched)


def test_unknown_workspace_errors(manager: WorkspaceManager) -> None:
    """Operations on unknown workspaces error clearly."""
    with pytest.raises(WorkspaceError, match="Unknown workspace"):
        manager.list_files("nope--nope--default")


def test_ttl_sweep_removes_expired(
    manager: WorkspaceManager, fetched: str, settings: RepoStudySettings
) -> None:
    """The sweep deletes workspaces past their TTL."""
    meta = Path(settings.data_dir) / f"{fetched}.meta.json"
    expired = time.time() - (settings.ttl_minutes + 1) * 60
    meta.write_text(json.dumps({"fetched_at": expired}))
    manager.sweep()
    assert not (Path(settings.data_dir) / fetched).exists()
    assert not meta.exists()


def test_ttl_sweep_keeps_fresh(
    manager: WorkspaceManager, fetched: str, settings: RepoStudySettings
) -> None:
    """The sweep leaves fresh workspaces alone."""
    manager.sweep()
    assert (Path(settings.data_dir) / fetched).exists()


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def test_factory_disabled_returns_no_tools() -> None:
    """The factory returns no tools when repo_study is disabled."""
    assert build_repo_study_tools(RepoStudySettings(), DirectRepoSettings()) == []


@pytest.mark.asyncio
async def test_factory_tools_relay_errors_as_strings(tmp_path: Path) -> None:
    """Tool wrappers convert WorkspaceError into 'Error: …' strings."""
    tools = build_repo_study_tools(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")),
        DirectRepoSettings(),
    )
    names = [t.__name__ for t in tools]
    assert names == [
        "fetch_repo_for_study",
        "list_repo_files",
        "read_repo_file",
        "search_repo_files",
        "drop_repo_workspace",
    ]
    by_name = dict(zip(names, tools, strict=True))
    assert (await by_name["fetch_repo_for_study"]("bad name")).startswith("Error:")
    assert (await by_name["list_repo_files"]("missing")).startswith("Error:")
    assert (await by_name["read_repo_file"]("missing", "x")).startswith("Error:")
    assert (await by_name["search_repo_files"]("missing", "x")).startswith("Error:")
    assert (await by_name["drop_repo_workspace"]("missing")).startswith("Error:")


@respx.mock
@pytest.mark.asyncio
async def test_factory_end_to_end_fetch_then_read(tmp_path: Path) -> None:
    """Fetch-then-read works end to end through the tool wrappers."""
    tools = build_repo_study_tools(
        RepoStudySettings(enabled=True, data_dir=str(tmp_path / "ws")),
        DirectRepoSettings(),
    )
    by_name = {t.__name__: t for t in tools}
    respx.get(TARBALL_URL).mock(
        return_value=Response(200, content=make_tarball(SAMPLE_FILES))
    )
    summary = await by_name["fetch_repo_for_study"]("acme/widget")
    assert "acme--widget--default" in summary
    out = await by_name["read_repo_file"]("acme--widget--default", "README.md")
    assert "# Widget" in out
