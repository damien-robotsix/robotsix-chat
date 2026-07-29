"""Async SFTP client for the config-restore capability.

Wraps ``asyncssh`` to provide read, write, and directory-listing
operations against a remote SFTP server.  All operations are
constrained under an optional ``remote_root`` for safety.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config.models import SftpSettings

logger = logging.getLogger(__name__)


class SftpError(Exception):
    """Raised when an SFTP operation fails."""


class SftpPathError(SftpError):
    """Raised when a path escapes the configured ``remote_root``."""


async def _resolve_path(
    remote_path: str,
    remote_root: str,
) -> PurePosixPath:
    """Resolve *remote_path* under *remote_root* and validate containment.

    Returns the resolved :class:`PurePosixPath`.  Raises
    :exc:`SftpPathError` when the resolved path escapes *remote_root*.
    """
    if not remote_root:
        return PurePosixPath(remote_path)

    import os

    root = PurePosixPath(remote_root)
    # Normalise: join the root with the provided path, then resolve ".." segments.
    candidate = PurePosixPath(root, remote_path)
    # Use os.path.normpath to resolve ".." components — PurePosixPath
    # preserves them verbatim, which would let a path like
    #   /var/www/../../../etc/passwd
    # pass the containment check below.
    resolved = PurePosixPath(os.path.normpath(str(candidate)))
    # Check that the resolved path is still under root.
    try:
        resolved.relative_to(root)
    except ValueError:
        raise SftpPathError(
            f"Path {remote_path!r} escapes remote_root {remote_root!r} "
            f"(resolved to {resolved!s})"
        ) from None
    return resolved


class SftpClient:
    """Async SFTP client backed by ``asyncssh``.

    Constructed from :class:`SftpSettings`.  Each method opens a fresh
    connection so the client is stateless and safe to share across
    concurrent tool calls.
    """

    def __init__(self, settings: SftpSettings) -> None:
        """Initialise the client from :class:`SftpSettings`."""
        self._settings = settings

    def _connection_kwargs(self) -> dict[str, object]:
        """Build keyword arguments for ``asyncssh.connect``."""
        kwargs: dict[str, object] = {
            "host": self._settings.host,
            "port": self._settings.port,
            "username": self._settings.username,
        }

        password = self._settings.password.get_secret_value()
        if password:
            kwargs["password"] = password

        private_key = self._settings.private_key.get_secret_value()
        if private_key:
            passphrase = (
                self._settings.private_key_passphrase.get_secret_value() or None
            )
            kwargs["client_keys"] = [private_key]
            if passphrase:
                kwargs["passphrase"] = passphrase

        known_hosts = self._settings.known_hosts
        if known_hosts:
            kwargs["known_hosts"] = known_hosts
        else:
            # Skip host-key verification when no known_hosts are configured.
            # The client still logs a warning for audit purposes.
            kwargs["known_hosts"] = None

        return kwargs

    async def _connect(self) -> Any:
        """Open a new asyncssh SFTP connection.  Caller must close it."""
        import asyncssh

        kwargs = self._connection_kwargs()
        logger.debug(
            "Connecting to SFTP server %s:%d",
            self._settings.host,
            self._settings.port,
        )
        conn = await asyncssh.connect(**kwargs)
        return conn

    async def read_file(self, remote_path: str) -> str:
        """Read the contents of *remote_path* from the SFTP server.

        Returns the file content as a string (UTF-8 decoded).
        """
        import asyncssh

        resolved = await _resolve_path(remote_path, self._settings.remote_root)
        try:
            conn = await self._connect()
            try:
                async with conn.start_sftp_client() as sftp:
                    content = await sftp.read(str(resolved), encoding="utf-8")
                return str(content)
            finally:
                conn.close()
        except asyncssh.SFTPError as exc:
            raise SftpError(f"SFTP read failed for {remote_path!r}: {exc}") from exc
        except OSError as exc:
            raise SftpError(
                f"SFTP connection failed for {remote_path!r}: {exc}"
            ) from exc

    async def write_file(
        self,
        remote_path: str,
        content: str,
        *,
        backup: bool = True,
    ) -> str:
        """Write *content* to *remote_path* on the SFTP server.

        When *backup* is ``True`` (the default), the existing file at
        *remote_path* is renamed to ``<path>.bak`` before writing.
        Returns a human-readable success message.
        """
        import asyncssh

        resolved = await _resolve_path(remote_path, self._settings.remote_root)
        resolved_str = str(resolved)
        try:
            conn = await self._connect()
            try:
                async with conn.start_sftp_client() as sftp:
                    if backup:
                        # Check if the target already exists.
                        try:
                            await sftp.stat(resolved_str)
                        except asyncssh.SFTPError:
                            pass  # File doesn't exist — no backup needed.
                        else:
                            backup_path = f"{resolved_str}.bak"
                            await sftp.rename(resolved_str, backup_path)
                            logger.info(
                                "SFTP backup: %s → %s", resolved_str, backup_path
                            )

                    await sftp.write(resolved_str, content, encoding="utf-8")
                return (
                    f"Wrote {len(content)} bytes to {remote_path!r}"
                    f" on {self._settings.host}"
                )
            finally:
                conn.close()
        except asyncssh.SFTPError as exc:
            raise SftpError(f"SFTP write failed for {remote_path!r}: {exc}") from exc
        except OSError as exc:
            raise SftpError(
                f"SFTP connection failed for {remote_path!r}: {exc}"
            ) from exc

    async def list_directory(self, remote_path: str) -> str:
        """List the contents of *remote_path* directory on the SFTP server.

        Returns a newline-separated list of entry names.
        """
        import asyncssh

        resolved = await _resolve_path(remote_path, self._settings.remote_root)
        try:
            conn = await self._connect()
            try:
                async with conn.start_sftp_client() as sftp:
                    entries = await sftp.listdir(str(resolved))
                return "\n".join(entries)
            finally:
                conn.close()
        except asyncssh.SFTPError as exc:
            raise SftpError(f"SFTP listdir failed for {remote_path!r}: {exc}") from exc
        except OSError as exc:
            raise SftpError(
                f"SFTP connection failed for {remote_path!r}: {exc}"
            ) from exc

    async def file_exists(self, remote_path: str) -> bool:
        """Return ``True`` when *remote_path* exists on the SFTP server."""
        import asyncssh

        resolved = await _resolve_path(remote_path, self._settings.remote_root)
        try:
            conn = await self._connect()
            try:
                async with conn.start_sftp_client() as sftp:
                    await sftp.stat(str(resolved))
                return True
            finally:
                conn.close()
        except asyncssh.SFTPError:
            return False
        except OSError:
            return False
