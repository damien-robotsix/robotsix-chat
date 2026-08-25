"""HTTP client for the robotsix-file-hub service.

Provides async methods for downloading, uploading, and listing files
via the file-hub REST API (``GET /files/{id}``, ``POST /files``,
``GET /files/{id}/metadata``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from robotsix_chat.config.models import FileHubToolsSettings

logger = logging.getLogger(__name__)

# Hard ceiling on filename length after sanitisation.
_MAX_FILENAME_LEN = 200


class FileHubError(Exception):
    """Base exception for file-hub operations."""


class FileHubNotFoundError(FileHubError):
    """The requested file id does not exist on file-hub."""


class FileHubUnavailableError(FileHubError):
    """The file-hub service is unreachable."""


class FileHubClient:
    """Async HTTP client for robotsix-file-hub.

    Args:
        settings: FileHubToolsSettings providing base_url and timeout.

    """

    def __init__(self, settings: FileHubToolsSettings) -> None:
        """Initialize the client from settings."""
        self._base_url = settings.base_url.rstrip("/")
        self._timeout = settings.timeout
        self._max_download = settings.max_download_bytes

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            follow_redirects=True,
        )

    async def download_file(
        self,
        file_id: str,
        dest_dir: Path,
    ) -> tuple[Path, dict[str, Any]]:
        """Download a file from file-hub by *file_id* into *dest_dir*.

        Returns:
            A tuple of ``(local_path, metadata)`` where *metadata* is the
            dict from the ``GET /files/{id}/metadata`` endpoint.

        Raises:
            FileHubNotFoundError: The file id does not exist.
            FileHubUnavailableError: file-hub is unreachable.

        """
        dest_dir.mkdir(parents=True, exist_ok=True)

        async with self._client() as client:
            # Fetch metadata first to get the filename.
            try:
                meta_resp = await client.get(f"/files/{file_id}/metadata")
            except httpx.ConnectError as exc:
                raise FileHubUnavailableError(
                    f"Cannot connect to file-hub at {self._base_url}: {exc}"
                ) from exc
            except httpx.TimeoutException as exc:
                raise FileHubUnavailableError(
                    f"Timeout connecting to file-hub at {self._base_url}: {exc}"
                ) from exc

            if meta_resp.status_code == 404:
                raise FileHubNotFoundError(f"File {file_id!r} not found on file-hub.")
            meta_resp.raise_for_status()
            metadata: dict[str, Any] = meta_resp.json()

            # Download the raw bytes.
            try:
                dl_resp = await client.get(
                    f"/files/{file_id}",
                    follow_redirects=True,
                )
            except httpx.ConnectError as exc:
                raise FileHubUnavailableError(
                    f"Cannot connect to file-hub at {self._base_url}: {exc}"
                ) from exc
            except httpx.TimeoutException as exc:
                raise FileHubUnavailableError(
                    f"Timeout downloading from file-hub at {self._base_url}: {exc}"
                ) from exc

            if dl_resp.status_code == 404:
                raise FileHubNotFoundError(f"File {file_id!r} not found on file-hub.")
            dl_resp.raise_for_status()

            content = dl_resp.content
            if len(content) > self._max_download:
                raise FileHubError(
                    f"File {file_id} is {len(content)} bytes, "
                    f"exceeding the {self._max_download} byte limit."
                )

            filename = _sanitize_filename(metadata.get("filename", file_id), file_id)
            local_path = dest_dir / filename
            # Avoid overwriting — append a counter if the file exists.
            local_path = _unique_path(local_path)
            local_path.write_bytes(content)

            logger.info(
                "Downloaded file-hub %s → %s (%d bytes)",
                file_id,
                local_path,
                len(content),
            )
            return local_path, metadata

    async def upload_file(
        self,
        file_path: Path,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file to file-hub.

        Returns:
            The upload response dict (includes ``id``, ``filename``,
            ``size``, ``content_type``, etc.).

        Raises:
            FileHubError: The file does not exist locally.
            FileHubUnavailableError: file-hub is unreachable.

        """
        if not file_path.exists():
            raise FileHubError(f"Local file not found: {file_path}")

        filename = file_path.name
        ct = content_type or _guess_content_type(filename)

        async with self._client() as client:
            try:
                with file_path.open("rb") as fh:
                    resp = await client.post(
                        "/files",
                        files={"file": (filename, fh, ct)},
                    )
            except httpx.ConnectError as exc:
                raise FileHubUnavailableError(
                    f"Cannot connect to file-hub at {self._base_url}: {exc}"
                ) from exc
            except httpx.TimeoutException as exc:
                raise FileHubUnavailableError(
                    f"Timeout uploading to file-hub at {self._base_url}: {exc}"
                ) from exc

            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            logger.info("Uploaded %s → file-hub id %s", file_path, result.get("id"))
            return result


def _sanitize_filename(name: str, fallback: str) -> str:
    """Sanitise a filename, stripping path components and limiting length."""
    # Take only the basename to prevent directory traversal.
    name = Path(name).name
    if not name or name == ".":
        name = fallback
    # Truncate to max length while preserving the extension.
    if len(name) > _MAX_FILENAME_LEN:
        ext = Path(name).suffix
        stem = name[: _MAX_FILENAME_LEN - len(ext)]
        name = stem + ext
    return name


def _unique_path(path: Path) -> Path:
    """Return *path*, appending a counter suffix if it already exists."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    # Fallback — unlikely but safe.
    return path


def _guess_content_type(filename: str) -> str:
    """Guess a MIME type from the file extension."""
    ext = Path(filename).suffix.lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
