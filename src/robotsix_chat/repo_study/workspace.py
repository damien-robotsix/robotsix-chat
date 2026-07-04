"""Temporary repo workspaces — tarball fetch, safe extraction, local study.

Downloads a GitHub repository snapshot as a tarball (no ``git`` binary — the
runtime image ships none), extracts it under the configured data directory,
and offers read-only listing / reading / regex search over the extracted
tree.  Workspaces are transient: a TTL sweep runs on every operation and an
explicit drop deletes one immediately.

Authentication reuses the ``direct_repo`` GitHub App credentials when
configured; otherwise only public repositories are reachable.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings, RepoStudySettings

logger = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_META_SUFFIX = ".meta.json"

# Files larger than this are skipped by the regex search (binary blobs,
# vendored bundles) so a search never stalls on huge files.
_SEARCH_MAX_FILE_BYTES = 1_048_576


def _workspace_id(repo: str, ref: str) -> str:
    """Deterministic workspace id for *repo* at *ref* (``owner--name--ref``)."""
    owner, name = repo.split("/", 1)
    ref_part = _ID_SANITIZE_RE.sub("-", ref) if ref else "default"
    return f"{owner}--{name}--{ref_part}"


class WorkspaceError(Exception):
    """A repo-study operation failed; the message is agent-relayable."""


class WorkspaceManager:
    """Fetches, sweeps, and serves temporary repo workspaces."""

    def __init__(
        self,
        settings: RepoStudySettings,
        direct_repo: DirectRepoSettings,
    ) -> None:
        """Store settings; the data directory is created lazily on first use."""
        self._s = settings
        self._direct_repo = direct_repo
        self._root = Path(settings.data_dir)

    # -- auth ---------------------------------------------------------------

    async def _auth_headers(self) -> dict[str, str]:
        """GitHub API headers, with an App installation token when configured.

        Reuses the ``direct_repo`` GitHub App credentials (JWT → installation
        token, cached in :mod:`robotsix_chat.direct_repo.client`).  Falls back
        to unauthenticated headers — public repos only — when the App is not
        configured or the token exchange fails.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        dr = self._direct_repo
        if (
            dr.github_app_id
            and dr.github_app_private_key.get_secret_value()
            and dr.github_app_installation_id
        ):
            from robotsix_chat.direct_repo.client import _get_installation_token

            try:
                token = await _get_installation_token(dr)
                headers["Authorization"] = f"Bearer {token}"
            except RuntimeError as exc:
                logger.warning(
                    "repo_study: GitHub App token unavailable, "
                    "falling back to unauthenticated fetch: %s",
                    exc,
                )
        return headers

    # -- TTL sweep ----------------------------------------------------------

    def sweep(self) -> None:
        """Delete every workspace older than the configured TTL."""
        if not self._root.is_dir():
            return
        cutoff = time.time() - self._s.ttl_minutes * 60
        for meta_path in self._root.glob(f"*{_META_SUFFIX}"):
            try:
                fetched_at = float(
                    json.loads(meta_path.read_text()).get("fetched_at", 0)
                )
            except json.JSONDecodeError, OSError, ValueError:
                fetched_at = 0.0
            if fetched_at < cutoff:
                ws_id = meta_path.name.removesuffix(_META_SUFFIX)
                logger.info("repo_study: sweeping expired workspace %s", ws_id)
                self._delete(ws_id)

    def _delete(self, workspace_id: str) -> None:
        """Remove a workspace directory and its metadata file."""
        shutil.rmtree(self._root / workspace_id, ignore_errors=True)
        (self._root / f"{workspace_id}{_META_SUFFIX}").unlink(missing_ok=True)

    # -- fetch --------------------------------------------------------------

    async def fetch(self, repo: str, ref: str = "") -> str:
        """Download and extract *repo* at *ref*; return a workspace summary.

        Raises:
            WorkspaceError: on any validation, download, or extraction
                failure — the message is safe to relay to the agent.

        """
        self.sweep()
        if not _REPO_RE.match(repo):
            raise WorkspaceError(
                f"Invalid repo {repo!r} — expected 'owner/name' "
                "(GitHub repository full name)."
            )

        base = self._direct_repo.github_api_base_url.rstrip("/")
        url = (
            f"{base}/repos/{repo}/tarball/{ref}"
            if ref
            else (f"{base}/repos/{repo}/tarball")
        )
        archive = await self._download(url)
        workspace_id = _workspace_id(repo, ref)
        dest = self._root / workspace_id

        # Re-fetching the same repo+ref replaces the previous snapshot.
        self._delete(workspace_id)
        file_count = self._extract(archive, dest)

        self._root.mkdir(parents=True, exist_ok=True)
        meta = {"repo": repo, "ref": ref, "fetched_at": time.time()}
        (self._root / f"{workspace_id}{_META_SUFFIX}").write_text(json.dumps(meta))

        top_level = sorted(
            entry.name + ("/" if entry.is_dir() else "") for entry in dest.iterdir()
        )
        return (
            f"Workspace '{workspace_id}' ready: {repo}"
            f"{f' @ {ref}' if ref else ''} — {file_count} files.\n"
            f"Top level: {', '.join(top_level) or '(empty)'}\n"
            f"It is deleted after {self._s.ttl_minutes} minutes; "
            "drop it earlier with drop_repo_workspace."
        )

    async def _download(self, url: str) -> bytes:
        """Stream the tarball, enforcing the archive-size cap."""
        headers = await self._auth_headers()
        chunks: list[bytes] = []
        total = 0
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._s.timeout, follow_redirects=True
                ) as client,
                client.stream("GET", url, headers=headers) as response,
            ):
                if response.status_code >= 400:
                    raise WorkspaceError(
                        f"GitHub returned {response.status_code} for {url} — "
                        "check the repo name/ref and whether the GitHub App "
                        "installation can read it (public repos need no auth)."
                    )
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self._s.max_archive_bytes:
                        raise WorkspaceError(
                            f"Archive exceeds the "
                            f"{self._s.max_archive_bytes} byte limit — "
                            "this repo is too large to study locally."
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise WorkspaceError(f"Download failed: {exc}") from exc
        return b"".join(chunks)

    def _extract(self, archive: bytes, dest: Path) -> int:
        """Extract *archive* into *dest*, stripping the tarball's root prefix.

        Uses the stdlib ``data`` extraction filter, which rejects absolute
        paths, ``..`` traversal, symlinks/hardlinks pointing outside the
        tree, and device nodes.  Enforces the total-uncompressed-size cap.
        """
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                members: list[tarfile.TarInfo] = []
                total = 0
                for member in tar.getmembers():
                    # GitHub tarballs wrap everything in "<owner>-<repo>-<sha>/".
                    _, _, stripped = member.name.partition("/")
                    if not stripped:
                        continue
                    member.name = stripped
                    total += member.size
                    if total > self._s.max_extracted_bytes:
                        raise WorkspaceError(
                            f"Extracted size exceeds the "
                            f"{self._s.max_extracted_bytes} byte limit — "
                            "this repo is too large to study locally."
                        )
                    members.append(member)
                dest.mkdir(parents=True, exist_ok=True)
                tar.extractall(path=dest, members=members, filter="data")
                return sum(1 for m in members if m.isfile())
        except WorkspaceError:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        except (tarfile.TarError, OSError) as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise WorkspaceError(f"Extraction failed: {exc}") from exc

    # -- read-only study operations ------------------------------------------

    def _resolve_root(self, workspace_id: str) -> Path:
        """Return the workspace root, or raise if it does not exist."""
        self.sweep()
        root = self._root / workspace_id
        if "/" in workspace_id or "\\" in workspace_id or not root.is_dir():
            raise WorkspaceError(
                f"Unknown workspace {workspace_id!r} — fetch the repo first "
                "with fetch_repo_for_study (workspaces also expire after "
                f"{self._s.ttl_minutes} minutes)."
            )
        return root

    def _resolve_file(self, root: Path, path: str) -> Path:
        """Resolve *path* inside *root*, rejecting traversal outside it."""
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()):
            raise WorkspaceError(f"Path {path!r} escapes the workspace.")
        return target

    def list_files(
        self, workspace_id: str, glob: str = "**/*", max_entries: int = 500
    ) -> str:
        """List files matching *glob* (workspace-relative), with sizes."""
        root = self._resolve_root(workspace_id)
        lines: list[str] = []
        truncated = False
        for path in sorted(root.glob(glob)):
            if not path.is_file():
                continue
            if len(lines) >= max_entries:
                truncated = True
                break
            rel = path.relative_to(root)
            lines.append(f"{rel} ({path.stat().st_size} bytes)")
        if not lines:
            return f"No files match {glob!r} in workspace '{workspace_id}'."
        listing = "\n".join(lines)
        if truncated:
            listing += f"\n… truncated at {max_entries} entries — narrow the glob."
        return listing

    def read_file(
        self,
        workspace_id: str,
        path: str,
        start_line: int = 1,
        max_lines: int = 400,
    ) -> str:
        """Return a line-numbered slice of a workspace file."""
        root = self._resolve_root(workspace_id)
        target = self._resolve_file(root, path)
        if not target.is_file():
            raise WorkspaceError(
                f"No file {path!r} in workspace '{workspace_id}' — "
                "use list_repo_files to see what exists."
            )
        try:
            text = target.read_bytes()[: self._s.max_read_bytes].decode(
                "utf-8", errors="replace"
            )
        except OSError as exc:
            raise WorkspaceError(f"Cannot read {path!r}: {exc}") from exc
        lines = text.splitlines()
        start = max(start_line, 1)
        window = lines[start - 1 : start - 1 + max_lines]
        if not window:
            return f"{path}: no content at line {start} (file has {len(lines)} lines)."
        numbered = "\n".join(
            f"{n}\t{line}" for n, line in enumerate(window, start=start)
        )
        suffix = ""
        if start - 1 + max_lines < len(lines):
            suffix = (
                f"\n… truncated at line {start - 1 + max_lines} of {len(lines)} — "
                "re-read with a higher start_line."
            )
        return numbered + suffix

    def search(
        self,
        workspace_id: str,
        pattern: str,
        glob: str = "**/*",
        max_matches: int = 50,
    ) -> str:
        """Regex-search workspace files; return ``path:line: text`` matches."""
        root = self._resolve_root(workspace_id)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise WorkspaceError(f"Invalid regex {pattern!r}: {exc}") from exc
        matches: list[str] = []
        truncated = False
        for path in sorted(root.glob(glob)):
            if truncated:
                break
            if not path.is_file() or path.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in content:
                continue  # binary
            rel = path.relative_to(root)
            for lineno, line in enumerate(
                content.decode("utf-8", errors="replace").splitlines(), start=1
            ):
                if regex.search(line):
                    if len(matches) >= max_matches:
                        truncated = True
                        break
                    matches.append(f"{rel}:{lineno}: {line.strip()}")
        if not matches:
            return (
                f"No matches for {pattern!r} (glob {glob!r}) "
                f"in workspace '{workspace_id}'."
            )
        result = "\n".join(matches)
        if truncated:
            result += (
                f"\n… truncated at {max_matches} matches — narrow the pattern or glob."
            )
        return result

    def drop(self, workspace_id: str) -> str:
        """Delete a workspace immediately."""
        root = self._resolve_root(workspace_id)
        self._delete(root.name)
        return f"Workspace '{workspace_id}' deleted."
