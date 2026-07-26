"""SFTP config-restore tools for the agent.

Exposes :func:`build_sftp_tools` — a factory returning read, list, and
(confirmation-gated) write tools for a remote SFTP server.  Returns no
tools when disabled, so the agent runs exactly as before.

Write operations are **confirmation-gated** — the agent must present the
exact file, target host/path, and diff to the operator for approval before
calling any write tool.  This gate is mandatory and cannot be bypassed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config.models import SftpSettings

__all__ = ["build_sftp_tools"]


def build_sftp_tools(
    settings: SftpSettings,
) -> list[Callable[..., Any]]:
    """Return SFTP config-restore tools, or an empty list when disabled.

    Args:
        settings: SftpSettings configuration (``enabled`` master switch,
            host, port, credentials, remote_root).

    Returns:
        A list of async callables (``sftp_read_file``,
        ``sftp_write_file``, ``sftp_list_directory``), or ``[]`` when
        *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    # Defer the import so the optional asyncssh dependency is only
    # loaded when the feature is actually enabled.
    from robotsix_chat.sftp.client import SftpClient, SftpError, SftpPathError

    client = SftpClient(settings)

    async def sftp_read_file(
        remote_path: str,
    ) -> str:
        """Read a file from the remote SFTP server.

        Returns the file content as a UTF-8 string.  When the file does
        not exist or is unreadable, returns an error message.

        Args:
            remote_path: Absolute or relative path to the file on the
                remote server.  When ``remote_root`` is configured, the
                path is resolved relative to it and cannot escape.

        Returns:
            The file content, or an error description.

        """
        try:
            content = await client.read_file(remote_path)
        except (SftpError, SftpPathError) as exc:
            return f"SFTP read error: {exc}"
        except ImportError:
            return (
                "SFTP tools require the ``asyncssh`` package. "
                "Install it with: pip install asyncssh"
            )
        return content

    async def sftp_write_file(
        remote_path: str,
        content: str,
        backup: bool = True,
    ) -> str:
        """Write a file to the remote SFTP server.

        **Confirmation-gated.**  Before calling, confirm the exact file,
        target host and path, and the diff / content with the operator
        in-chat.  This gate is mandatory and cannot be bypassed or
        auto-approved — every production write requires explicit operator
        approval.

        When *backup* is ``True`` (the default), any existing file at the
        target path is renamed to ``<path>.bak`` before the write.

        Args:
            remote_path: Absolute or relative path to the target file on
                the remote server.  When ``remote_root`` is configured,
                the path is resolved relative to it and cannot escape.
            content: The full file content to write (plain text, UTF-8).
            backup: When ``True`` (default), rename the existing file to
                ``<path>.bak`` before writing.  Set to ``False`` to
                overwrite without a backup.

        Returns:
            A success message including byte count and target path, or an
            error description.

        """
        try:
            result = await client.write_file(remote_path, content, backup=backup)
        except (SftpError, SftpPathError) as exc:
            return f"SFTP write error: {exc}"
        except ImportError:
            return (
                "SFTP tools require the ``asyncssh`` package. "
                "Install it with: pip install asyncssh"
            )
        return result

    async def sftp_list_directory(
        remote_path: str,
    ) -> str:
        """List the contents of a directory on the remote SFTP server.

        Returns a newline-separated list of entry names.  When the
        directory does not exist or is unreadable, returns an error
        message.

        Args:
            remote_path: Absolute or relative path to the directory on the
                remote server.

        Returns:
            A newline-separated list of entries, or an error description.

        """
        try:
            entries = await client.list_directory(remote_path)
        except (SftpError, SftpPathError) as exc:
            return f"SFTP list error: {exc}"
        except ImportError:
            return (
                "SFTP tools require the ``asyncssh`` package. "
                "Install it with: pip install asyncssh"
            )
        if not entries:
            return f"(empty directory: {remote_path!r})"
        return entries

    async def sftp_file_exists(
        remote_path: str,
    ) -> str:
        """Check whether a file or directory exists on the remote SFTP server.

        Returns ``"true"`` or ``"false"`` — suitable for use as a
        pre-check before attempting a write or restore.

        Args:
            remote_path: Path to check on the remote server.

        Returns:
            ``"true"`` when the path exists, ``"false"`` otherwise.

        """
        try:
            exists = await client.file_exists(remote_path)
        except SftpError, SftpPathError:
            return "false"
        except ImportError:
            return (
                "SFTP tools require the ``asyncssh`` package. "
                "Install it with: pip install asyncssh"
            )
        return "true" if exists else "false"

    return [sftp_read_file, sftp_write_file, sftp_list_directory, sftp_file_exists]
