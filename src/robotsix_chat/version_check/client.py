"""GitHub-Releases-API client for the self-version-check tool.

Read-only: only ``GET /repos/{owner}/{name}/releases/latest`` (with a
fallback to ``GET /repos/{owner}/{name}/tags`` when no latest release
exists) is called, with optional Bearer auth. All HTTP/network errors are
caught and returned as concise strings — nothing raises to the agent loop.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from robotsix_chat.common.http import safe_http_request
from robotsix_chat.config import VersionCheckSettings

logger = logging.getLogger(__name__)


def _parse_version(s: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of integers for comparison.

    Strips a leading ``v`` / ``V``, splits on ``.``, and converts
    leading-numeric components to ints.  Non-numeric segments (e.g. a
    pre-release suffix like ``-rc1``) are ignored — parsing stops at the
    first component that cannot be converted to an int.

    Returns ``()`` when *s* cannot be parsed into at least one integer.

        >>> _parse_version("v1.2.3")
        (1, 2, 3)
        >>> _parse_version("1.2.0-rc1")
        (1, 2, 0)
        >>> _parse_version("not-a-version")
        ()

    """
    raw = s.strip().lstrip("vV")
    if not raw:
        return ()
    parts: list[int] = []
    for part in raw.split("."):
        # Extract the leading numeric portion (handles "0-rc1" → 0).
        # If the part contains any non-digit, treat it as a pre-release
        # marker and stop consuming further components.
        numeric = ""
        has_non_digit = False
        for ch in part:
            if ch.isdigit():
                numeric += ch
            else:
                has_non_digit = True
                break
        if not numeric:
            break
        parts.append(int(numeric))
        if has_non_digit:
            break
    if not parts:
        return ()
    return tuple(parts)


def compare_versions(current: str, latest: str) -> bool:
    """Return ``True`` when *current* is up-to-date (>= *latest*).

    When both strings parse successfully via :func:`_parse_version` they
    are compared as integer tuples.  Otherwise the comparison falls back
    to exact string equality (non-semantic).
    """
    cur_tup = _parse_version(current)
    lat_tup = _parse_version(latest)
    if cur_tup and lat_tup:
        return cur_tup >= lat_tup
    # Non-semantic fallback — note the comparison is literal, not numeric.
    return current == latest


class VersionCheckClient:
    """Fetch the latest version from a GitHub repo's releases / tags.

    Caches the successful result on the instance for *cache_ttl* seconds
    (monotonic clock) to avoid hammering the API across rapid agent calls.
    Failed lookups are never cached.
    """

    def __init__(self, settings: VersionCheckSettings) -> None:
        """Store *settings* and normalise the base URL for later calls."""
        self._s = settings
        self._base_url = settings.base_url.rstrip("/")
        self._cache_ts: float | None = None
        self._cache_value: str | None = None

    async def latest_version(self) -> tuple[str | None, str]:
        """Return ``(version_or_None, source_or_error_detail)``.

        The first element is the version string when the lookup succeeds,
        or ``None`` on failure.  The second element is a human-readable
        note: the data source (``"releases/latest"`` or ``"tags"``) on
        success, or an error detail on failure.  The method never raises.
        """
        # --- cache check (monotonic clock) ---
        now = time.monotonic()
        if (
            self._cache_ts is not None
            and self._cache_value is not None
            and now - self._cache_ts < self._s.cache_ttl
        ):
            return self._cache_value, "cached"

        version, source = await self._fetch_latest()
        if version is not None:
            self._cache_ts = now
            self._cache_value = version
        return version, source

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fetch_latest(self) -> tuple[str | None, str]:
        """Hit the releases/latest endpoint; fall back to tags on 404."""
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
        }
        if self._s.github_token:
            headers["Authorization"] = f"Bearer {self._s.github_token}"

        url = f"{self._base_url}/repos/{self._s.repo}/releases/latest"
        result = await safe_http_request(
            "GET",
            url,
            headers=headers,
            timeout=self._s.timeout,
            label="GitHub API",
        )
        if result.error:
            if result.status_code == 404:
                # No published releases yet — try the tags endpoint.
                return await self._fetch_first_tag(headers)
            return None, result.error
        data: dict[str, Any] = json.loads(result.text)  # type: ignore[arg-type]
        tag = data.get("tag_name", "")
        if tag:
            return tag, "releases/latest"
        return None, "releases/latest response had no tag_name"

    async def _fetch_first_tag(
        self,
        headers: dict[str, str],
    ) -> tuple[str | None, str]:
        """Fallback: GET /repos/{repo}/tags and return the first tag name."""
        url = f"{self._base_url}/repos/{self._s.repo}/tags"
        result = await safe_http_request(
            "GET",
            url,
            headers=headers,
            timeout=self._s.timeout,
            label="tags endpoint",
        )
        if result.error:
            return None, result.error
        data: list[dict[str, Any]] = json.loads(result.text)  # type: ignore[arg-type]
        if data and isinstance(data, list):
            first = data[0].get("name", "")
            if first:
                return first, "tags (no releases found)"
        return None, "no tags or releases found in repo"
