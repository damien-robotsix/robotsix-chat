"""HTTP client for the GitHub REST API (repo management).

Calls the GitHub REST API over HTTPS with ``Authorization: Bearer``
token auth.  All methods return strings — success payloads and error
messages alike — so nothing raises into the agent loop.
"""

from __future__ import annotations

import json
import logging

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.config import GithubSettings

logger = logging.getLogger(__name__)


class GitHubClient:
    """HTTP client for the GitHub REST API (repo management subset)."""

    def __init__(self, settings: GithubSettings) -> None:
        """Initialise with GitHub settings."""
        self._s = settings
        self._base_url = settings.api_base_url.rstrip("/")

    # -- public methods ---------------------------------------------------

    async def create_repo(self, name: str, description: str, visibility: str) -> str:
        """``POST /user/repos`` — create a personal repository.

        Uses the authenticated user's account.  For org repos the caller
        should use ``POST /orgs/{org}/repos`` — not yet implemented.
        """
        body: dict[str, object] = {
            "name": name,
            "private": visibility == "private",
        }
        if description:
            body["description"] = description
        return await self._post("/user/repos", body)

    async def update_repo(
        self,
        owner: str,
        repo: str,
        description: str | None,
        visibility: str | None,
        has_issues: bool | None,
        has_wiki: bool | None,
    ) -> str:
        """``PATCH /repos/{owner}/{repo}`` — update repository settings."""
        body: dict[str, object] = {}
        if description is not None:
            body["description"] = description
        if visibility is not None:
            if visibility not in ("private", "public"):
                return "Error: visibility must be 'private' or 'public'"
            body["private"] = visibility == "private"
        if has_issues is not None:
            body["has_issues"] = has_issues
        if has_wiki is not None:
            body["has_wiki"] = has_wiki
        if not body:
            return "Error: at least one field to update must be provided"
        return await self._patch(f"/repos/{owner}/{repo}", body)

    async def get_repo(self, owner: str, repo: str) -> str:
        """``GET /repos/{owner}/{repo}`` — read repository details."""
        return await self._get(f"/repos/{owner}/{repo}")

    # -- internals --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        token = self._s.token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _get(self, path: str) -> str:
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            label="GitHub API",
        )
        if result.error:
            return result.error
        try:
            parsed = json.loads(str(result.text))
            return json.dumps(parsed, indent=2)
        except Exception:
            return str(result.text)

    async def _post(self, path: str, body: dict[str, object]) -> str:
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "POST",
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            json_body=body,
            label="GitHub API",
        )
        if result.error:
            return result.error
        try:
            parsed = json.loads(str(result.text))
            return json.dumps(parsed, indent=2)
        except Exception:
            return str(result.text)

    async def _patch(self, path: str, body: dict[str, object]) -> str:
        url = f"{self._base_url}{path}"
        result = await safe_http_request(
            "PATCH",
            url,
            headers=self._headers(),
            timeout=self._s.timeout,
            json_body=body,
            label="GitHub API",
        )
        if result.error:
            return result.error
        try:
            parsed = json.loads(str(result.text))
            return json.dumps(parsed, indent=2)
        except Exception:
            return str(result.text)
