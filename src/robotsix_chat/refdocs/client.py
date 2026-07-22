"""GitHub-Contents-API client for the reference-docs tool.

Read-only: only ``GET /repos/{owner}/{name}/contents/{path}?ref={ref}`` is
called, with optional Bearer auth. All HTTP/network errors are caught and
returned as concise strings — nothing raises to the agent loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.config import DirectRepoSettings, RefDocsSettings

logger = logging.getLogger(__name__)

# Truncation guard: returned file content is capped at this many characters.
# Larger files get a clear truncation marker appended so the model knows the
# content is incomplete without the file blowing the context window.
_MAX_CONTENT_CHARS = 30_000


class RefDocsClient:
    """Fetch files from allowlisted GitHub repos via the Contents API."""

    def __init__(
        self, settings: RefDocsSettings, direct_repo: DirectRepoSettings
    ) -> None:
        """Store *settings* and *direct_repo* for later calls."""
        self._s = settings
        self._dr = direct_repo
        # Normalise the base URL once so callers don't worry about trailing slashes.
        self._base_url = settings.base_url.rstrip("/")

    async def read_file(self, repo: str, path: str) -> str:
        """Return the text content of *path* in *repo*.

        Returns a concise error string (never raises) on any failure:
        repo not in the allowlist, HTTP/network errors, non-file path,
        or decoding problems.
        """
        data = await self._fetch_json(repo, path, "fetch")
        if isinstance(data, str):
            return data

        if isinstance(data, list):
            return (
                f"'{path}' is a directory in {repo}, not a file. "
                f"Use list_reference_docs to browse its contents."
            )
        content = data.get("content") if isinstance(data, dict) else None
        encoding = data.get("encoding") if isinstance(data, dict) else None
        if not content or encoding != "base64":
            return (
                f"Unable to decode {repo}/{path}: "
                "response is not a base64-encoded file."
            )
        try:
            text = base64.b64decode(content).decode("utf-8")
        except Exception as exc:
            return f"Failed to decode {repo}/{path}: {exc}"
        if len(text) > _MAX_CONTENT_CHARS:
            text = (
                text[:_MAX_CONTENT_CHARS]
                + f"\n\n... [truncated at {_MAX_CONTENT_CHARS} chars; "
                f"full file is {len(text)} chars]"
            )
        return text

    async def list_files(self, repo: str, path: str = "") -> str:
        """Return a compact listing of files/dirs at *path* in *repo*.

        Returns a concise error string (never raises) on any failure.
        """
        data = await self._fetch_json(repo, path, "list")
        if isinstance(data, str):
            return data

        if not isinstance(data, list):
            return (
                f"'{path or '/'}' is a file in {repo}, not a directory. "
                f"Use read_reference_doc to read its contents."
            )
        lines: list[str] = []
        for entry in data:
            name = entry.get("name", "?")
            etype = entry.get("type", "?")
            if etype == "dir":
                lines.append(f"  {name}/")
            else:
                lines.append(f"  {name}")
        header = f"{repo}/{path or '/'} ({len(lines)} entries):"
        return header + "\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fetch_json(self, repo: str, path: str, action: str) -> Any:
        """Fetch JSON from the GitHub Contents API for *repo*/*path*.

        Returns the parsed JSON on success, or an error string on failure.
        """
        if not self._repo_allowed(repo):
            return self._repo_denied(repo)
        url = self._build_url(repo, path)
        try:
            return await self._get_json(url)
        except Exception as exc:
            logger.debug("refdocs %s failed: %s", action, exc)
            return f"Failed to {action} {repo}/{path}: {exc}"

    def _repo_allowed(self, repo: str) -> bool:
        return repo in self._s.repos

    def _repo_denied(self, repo: str) -> str:
        allowed = ", ".join(self._s.repos) if self._s.repos else "(none)"
        return (
            f"Access denied: '{repo}' is not in the allowlisted reference-docs "
            f"repos. Allowed repos: {allowed}"
        )

    def _build_url(self, repo: str, path: str) -> str:
        s = self._s
        encoded_path = "/" + httpx.URL(path).path.lstrip("/") if path else ""
        return f"{self._base_url}/repos/{repo}/contents{encoded_path}?ref={s.ref}"

    async def _get_json(self, url: str) -> Any:
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if (
            self._dr.github_app_id
            and self._dr.github_app_private_key.get_secret_value()
            and self._dr.github_app_installation_id
        ):
            from robotsix_github_auth import mint_installation_token

            try:
                result = await asyncio.to_thread(
                    mint_installation_token,
                    app_id=self._dr.github_app_id,
                    private_key=self._dr.github_app_private_key.get_secret_value(),
                    installation_id=self._dr.github_app_installation_id,
                )
                headers["Authorization"] = f"Bearer {result.token}"
            except RuntimeError as exc:
                logger.warning(
                    "refdocs: GitHub App token unavailable, "
                    "falling back to unauthenticated fetch: %s",
                    exc,
                )
        result = await safe_http_request(
            "GET",
            url,
            headers=headers,
            timeout=self._s.timeout,
            label="RefDocs",
        )
        if result.error:
            raise RuntimeError(result.error)
        return json.loads(result.text)  # type: ignore[arg-type]
