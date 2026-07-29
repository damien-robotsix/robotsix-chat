"""Tests for the SFTP client module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

pytest.importorskip("asyncssh", reason="asyncssh is required for SFTP client tests")

from robotsix_chat.config.models import SftpSettings
from robotsix_chat.sftp.client import (
    SftpClient,
    SftpError,
    SftpPathError,
    _resolve_path,
)


class TestResolvePath:
    """Tests for the _resolve_path helper."""

    @pytest.mark.asyncio
    async def test_no_remote_root_returns_pure_path(self) -> None:
        """When remote_root is empty, the path is returned unchanged."""
        result = await _resolve_path("/some/file.txt", "")
        assert str(result) == "/some/file.txt"

    @pytest.mark.asyncio
    async def test_path_under_root_resolves(self) -> None:
        """A relative path is resolved under remote_root."""
        result = await _resolve_path("subdir/file.txt", "/var/www")
        assert str(result) == "/var/www/subdir/file.txt"

    @pytest.mark.asyncio
    async def test_relative_path_resolves_correctly(self) -> None:
        """A path with dot segments resolves correctly under remote_root."""
        result = await _resolve_path("./subdir/file.txt", "/var/www")
        assert str(result) == "/var/www/subdir/file.txt"

    @pytest.mark.asyncio
    async def test_path_escaping_root_raises_error(self) -> None:
        """A path that traverses above remote_root raises SftpPathError."""
        with pytest.raises(SftpPathError, match="escapes remote_root"):
            await _resolve_path("../../../etc/passwd", "/var/www")

    @pytest.mark.asyncio
    async def test_absolute_path_escaping_root_raises_error(self) -> None:
        """An absolute path outside remote_root raises SftpPathError."""
        with pytest.raises(SftpPathError, match="escapes remote_root"):
            await _resolve_path("/etc/passwd", "/var/www")

    @pytest.mark.asyncio
    async def test_empty_remote_root_accepts_any_path(self) -> None:
        """When remote_root is empty, any path (including escapes) is accepted."""
        result = await _resolve_path("../../../etc/passwd", "")
        assert str(result) == "../../../etc/passwd"


class TestSftpClientReadFile:
    """Tests for SftpClient.read_file."""

    @pytest.fixture(autouse=True)
    def _mock_connect(self, monkeypatch) -> None:
        self.mock_sftp = AsyncMock()
        self.mock_conn = MagicMock()
        self.mock_conn.close = MagicMock()
        self.mock_conn.start_sftp_client = MagicMock()
        self.mock_conn.start_sftp_client.return_value.__aenter__ = AsyncMock(
            return_value=self.mock_sftp
        )
        self.mock_conn.start_sftp_client.return_value.__aexit__ = AsyncMock(
            return_value=None
        )
        monkeypatch.setattr(
            SftpClient,
            "_connect",
            AsyncMock(return_value=self.mock_conn),
        )

    @staticmethod
    def _make_settings(**kwargs: object) -> SftpSettings:
        defaults: dict[str, object] = {
            "enabled": True,
            "host": "test.example.com",
            "port": 22,
            "username": "tester",
            "password": SecretStr("secret"),
            "remote_root": "/srv",
        }
        defaults.update(kwargs)
        return SftpSettings(**defaults)

    @pytest.mark.asyncio
    async def test_read_file_success(self) -> None:
        """A file is read successfully and its content returned as a string."""
        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.read = AsyncMock(return_value="file content")

        result = await client.read_file("config.txt")

        assert result == "file content"
        self.mock_sftp.read.assert_called_once_with("/srv/config.txt", encoding="utf-8")

    @pytest.mark.asyncio
    async def test_read_file_path_escape_raises_error(self) -> None:
        """A path escaping remote_root raises SftpPathError before connecting."""
        settings = self._make_settings()
        client = SftpClient(settings)

        with pytest.raises(SftpPathError, match="escapes remote_root"):
            await client.read_file("../../../etc/passwd")

    @pytest.mark.asyncio
    async def test_read_file_empty_remote_root(self) -> None:
        """When remote_root is empty, any path is accepted as-is."""
        settings = self._make_settings(remote_root="")
        client = SftpClient(settings)
        self.mock_sftp.read = AsyncMock(return_value="content")

        result = await client.read_file("/any/path.txt")

        assert result == "content"
        self.mock_sftp.read.assert_called_once_with("/any/path.txt", encoding="utf-8")

    @pytest.mark.asyncio
    async def test_read_file_sftp_error_translated(self) -> None:
        """An SFTPError from asyncssh is translated to SftpError."""
        import asyncssh

        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.read = AsyncMock(
            side_effect=asyncssh.SFTPError(asyncssh.FX_NO_SUCH_FILE, "No such file")
        )

        with pytest.raises(SftpError, match="SFTP read failed"):
            await client.read_file("missing.txt")

    @pytest.mark.asyncio
    async def test_read_file_permission_denied_translated(self) -> None:
        """A permission-denied SFTPError is translated to SftpError."""
        import asyncssh

        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.read = AsyncMock(
            side_effect=asyncssh.SFTPError(
                asyncssh.FX_PERMISSION_DENIED, "Permission denied"
            )
        )

        with pytest.raises(SftpError, match="SFTP read failed"):
            await client.read_file("secret.txt")

    @pytest.mark.asyncio
    async def test_read_file_os_error_translated(self) -> None:
        """An OSError during connect is translated to SftpError."""
        settings = self._make_settings()
        client = SftpClient(settings)
        SftpClient._connect = AsyncMock(side_effect=OSError("Connection refused"))

        with pytest.raises(SftpError, match="SFTP connection failed"):
            await client.read_file("any.txt")


class TestSftpClientWriteFile:
    """Tests for SftpClient.write_file."""

    @pytest.fixture(autouse=True)
    def _mock_connect(self, monkeypatch) -> None:
        self.mock_sftp = AsyncMock()
        self.mock_conn = MagicMock()
        self.mock_conn.close = MagicMock()
        self.mock_conn.start_sftp_client = MagicMock()
        self.mock_conn.start_sftp_client.return_value.__aenter__ = AsyncMock(
            return_value=self.mock_sftp
        )
        self.mock_conn.start_sftp_client.return_value.__aexit__ = AsyncMock(
            return_value=None
        )
        monkeypatch.setattr(
            SftpClient,
            "_connect",
            AsyncMock(return_value=self.mock_conn),
        )

    @staticmethod
    def _make_settings(**kwargs: object) -> SftpSettings:
        defaults: dict[str, object] = {
            "enabled": True,
            "host": "test.example.com",
            "port": 22,
            "username": "tester",
            "password": SecretStr("secret"),
            "remote_root": "/srv",
        }
        defaults.update(kwargs)
        return SftpSettings(**defaults)

    @pytest.mark.asyncio
    async def test_write_file_success_no_backup_needed(self) -> None:
        """When the target file does not exist, no backup is created."""
        import asyncssh

        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.stat = AsyncMock(
            side_effect=asyncssh.SFTPError(asyncssh.FX_NO_SUCH_FILE, "No such file")
        )
        self.mock_sftp.write = AsyncMock()

        result = await client.write_file("config.txt", "new content")

        assert "Wrote 11 bytes" in result
        assert "config.txt" in result
        self.mock_sftp.write.assert_called_once_with(
            "/srv/config.txt", "new content", encoding="utf-8"
        )
        self.mock_sftp.rename.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_file_with_backup(self) -> None:
        """When the target file exists, it is renamed to .bak before writing."""
        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.stat = AsyncMock(return_value=MagicMock())
        self.mock_sftp.rename = AsyncMock()
        self.mock_sftp.write = AsyncMock()

        result = await client.write_file("config.txt", "updated content")

        self.mock_sftp.rename.assert_called_once_with(
            "/srv/config.txt", "/srv/config.txt.bak"
        )
        self.mock_sftp.write.assert_called_once_with(
            "/srv/config.txt", "updated content", encoding="utf-8"
        )
        assert "Wrote 15 bytes" in result

    @pytest.mark.asyncio
    async def test_write_file_no_backup_when_disabled(self) -> None:
        """When backup=False, stat and rename are never called."""
        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.write = AsyncMock()

        result = await client.write_file("config.txt", "content", backup=False)

        self.mock_sftp.stat.assert_not_called()
        self.mock_sftp.rename.assert_not_called()
        self.mock_sftp.write.assert_called_once()
        assert "Wrote 7 bytes" in result

    @pytest.mark.asyncio
    async def test_write_file_path_escape_raises_error(self) -> None:
        """A path escaping remote_root raises SftpPathError before connecting."""
        settings = self._make_settings()
        client = SftpClient(settings)

        with pytest.raises(SftpPathError, match="escapes remote_root"):
            await client.write_file("../../../etc/passwd", "malicious")

    @pytest.mark.asyncio
    async def test_write_file_sftp_error_translated(self) -> None:
        """An SFTPError during write is translated to SftpError."""
        import asyncssh

        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.write = AsyncMock(
            side_effect=asyncssh.SFTPError(
                asyncssh.FX_PERMISSION_DENIED, "Permission denied"
            )
        )

        with pytest.raises(SftpError, match="SFTP write failed"):
            await client.write_file("config.txt", "content")


class TestSftpClientListDirectory:
    """Tests for SftpClient.list_directory."""

    @pytest.fixture(autouse=True)
    def _mock_connect(self, monkeypatch) -> None:
        self.mock_sftp = AsyncMock()
        self.mock_conn = MagicMock()
        self.mock_conn.close = MagicMock()
        self.mock_conn.start_sftp_client = MagicMock()
        self.mock_conn.start_sftp_client.return_value.__aenter__ = AsyncMock(
            return_value=self.mock_sftp
        )
        self.mock_conn.start_sftp_client.return_value.__aexit__ = AsyncMock(
            return_value=None
        )
        monkeypatch.setattr(
            SftpClient,
            "_connect",
            AsyncMock(return_value=self.mock_conn),
        )

    @staticmethod
    def _make_settings(**kwargs: object) -> SftpSettings:
        defaults: dict[str, object] = {
            "enabled": True,
            "host": "test.example.com",
            "port": 22,
            "username": "tester",
            "password": SecretStr("secret"),
            "remote_root": "/srv",
        }
        defaults.update(kwargs)
        return SftpSettings(**defaults)

    @pytest.mark.asyncio
    async def test_list_directory_success(self) -> None:
        """Directory entries are returned as a newline-separated string."""
        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.listdir = AsyncMock(
            return_value=["file1.txt", "file2.txt", "subdir"]
        )

        result = await client.list_directory("logs")

        assert result == "file1.txt\nfile2.txt\nsubdir"
        self.mock_sftp.listdir.assert_called_once_with("/srv/logs")

    @pytest.mark.asyncio
    async def test_list_directory_empty(self) -> None:
        """An empty directory returns an empty string."""
        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.listdir = AsyncMock(return_value=[])

        result = await client.list_directory("empty_dir")

        assert result == ""

    @pytest.mark.asyncio
    async def test_list_directory_path_escape_raises_error(self) -> None:
        """A path escaping remote_root raises SftpPathError."""
        settings = self._make_settings()
        client = SftpClient(settings)

        with pytest.raises(SftpPathError, match="escapes remote_root"):
            await client.list_directory("../../../etc")

    @pytest.mark.asyncio
    async def test_list_directory_sftp_error_translated(self) -> None:
        """An SFTPError during listdir is translated to SftpError."""
        import asyncssh

        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.listdir = AsyncMock(
            side_effect=asyncssh.SFTPError(
                asyncssh.FX_NO_SUCH_FILE, "No such directory"
            )
        )

        with pytest.raises(SftpError, match="SFTP listdir failed"):
            await client.list_directory("nonexistent")


class TestSftpClientFileExists:
    """Tests for SftpClient.file_exists."""

    @pytest.fixture(autouse=True)
    def _mock_connect(self, monkeypatch) -> None:
        self.mock_sftp = AsyncMock()
        self.mock_conn = MagicMock()
        self.mock_conn.close = MagicMock()
        self.mock_conn.start_sftp_client = MagicMock()
        self.mock_conn.start_sftp_client.return_value.__aenter__ = AsyncMock(
            return_value=self.mock_sftp
        )
        self.mock_conn.start_sftp_client.return_value.__aexit__ = AsyncMock(
            return_value=None
        )
        monkeypatch.setattr(
            SftpClient,
            "_connect",
            AsyncMock(return_value=self.mock_conn),
        )

    @staticmethod
    def _make_settings(**kwargs: object) -> SftpSettings:
        defaults: dict[str, object] = {
            "enabled": True,
            "host": "test.example.com",
            "port": 22,
            "username": "tester",
            "password": SecretStr("secret"),
            "remote_root": "/srv",
        }
        defaults.update(kwargs)
        return SftpSettings(**defaults)

    @pytest.mark.asyncio
    async def test_file_exists_true(self) -> None:
        """Returns True when the file exists on the remote server."""
        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.stat = AsyncMock(return_value=MagicMock())

        result = await client.file_exists("config.txt")

        assert result is True
        self.mock_sftp.stat.assert_called_once_with("/srv/config.txt")

    @pytest.mark.asyncio
    async def test_file_exists_false(self) -> None:
        """Returns False when the file does not exist (FX_NO_SUCH_FILE)."""
        import asyncssh

        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.stat = AsyncMock(
            side_effect=asyncssh.SFTPError(asyncssh.FX_NO_SUCH_FILE, "No such file")
        )

        result = await client.file_exists("missing.txt")

        assert result is False

    @pytest.mark.asyncio
    async def test_file_exists_connection_error_returns_false(self) -> None:
        """Returns False when the connection itself fails (OSError)."""
        settings = self._make_settings()
        client = SftpClient(settings)
        SftpClient._connect = AsyncMock(side_effect=OSError("Connection refused"))

        result = await client.file_exists("any.txt")

        assert result is False

    @pytest.mark.asyncio
    async def test_file_exists_path_escape_raises_error(self) -> None:
        """A path escaping remote_root raises SftpPathError before any I/O."""
        settings = self._make_settings()
        client = SftpClient(settings)

        with pytest.raises(SftpPathError, match="escapes remote_root"):
            await client.file_exists("../../../etc/passwd")

    @pytest.mark.asyncio
    async def test_file_exists_permission_denied_returns_false(self) -> None:
        """Returns False when stat fails with permission denied."""
        import asyncssh

        settings = self._make_settings()
        client = SftpClient(settings)
        self.mock_sftp.stat = AsyncMock(
            side_effect=asyncssh.SFTPError(
                asyncssh.FX_PERMISSION_DENIED, "Permission denied"
            )
        )

        result = await client.file_exists("secret.txt")

        assert result is False


class TestSftpClientConnectionKwargs:
    """Tests for SftpClient._connection_kwargs without asyncssh."""

    @pytest.mark.asyncio
    async def test_password_auth(self) -> None:
        """Password authentication sets password and defaults known_hosts to None."""
        settings = SftpSettings(
            enabled=True,
            host="sftp.example.com",
            port=2222,
            username="admin",
            password=SecretStr("p@ss"),
        )
        client = SftpClient(settings)

        kwargs = client._connection_kwargs()

        assert kwargs["host"] == "sftp.example.com"
        assert kwargs["port"] == 2222
        assert kwargs["username"] == "admin"
        assert kwargs["password"] == "p@ss"  # pragma: allowlist secret
        assert kwargs["known_hosts"] is None

    @pytest.mark.asyncio
    async def test_key_auth_with_passphrase(self) -> None:
        """Key auth with passphrase includes client_keys and passphrase."""
        settings = SftpSettings(
            enabled=True,
            host="sftp.example.com",
            username="admin",
            private_key=SecretStr("-----BEGIN OPENSSH PRIVATE KEY-----"),
            private_key_passphrase=SecretStr("keypass"),
            known_hosts="sftp.example.com ssh-rsa AAA...",
        )
        client = SftpClient(settings)

        kwargs = client._connection_kwargs()

        assert kwargs["username"] == "admin"
        assert "password" not in kwargs
        assert (
            "-----BEGIN OPENSSH PRIVATE KEY-----" in kwargs["client_keys"]
        )  # pragma: allowlist secret
        assert kwargs["passphrase"] == "keypass"
        assert kwargs["known_hosts"] == "sftp.example.com ssh-rsa AAA..."

    @pytest.mark.asyncio
    async def test_key_auth_no_passphrase(self) -> None:
        """Key auth without passphrase omits the passphrase kwarg."""
        settings = SftpSettings(
            enabled=True,
            host="sftp.example.com",
            username="admin",
            private_key=SecretStr("key-material"),
        )
        client = SftpClient(settings)

        kwargs = client._connection_kwargs()

        assert kwargs["client_keys"] == ["key-material"]
        assert "passphrase" not in kwargs

    @pytest.mark.asyncio
    async def test_no_auth(self) -> None:
        """When neither password nor key is provided, both are omitted."""
        settings = SftpSettings(
            enabled=True,
            host="sftp.example.com",
            username="admin",
        )
        client = SftpClient(settings)

        kwargs = client._connection_kwargs()

        assert "password" not in kwargs
        assert "client_keys" not in kwargs

    @pytest.mark.asyncio
    async def test_password_auth_empty_string_omitted(self) -> None:
        """An empty password string is treated as absent (not sent)."""
        settings = SftpSettings(
            enabled=True,
            host="sftp.example.com",
            username="admin",
            password=SecretStr(""),
        )
        client = SftpClient(settings)

        kwargs = client._connection_kwargs()

        assert "password" not in kwargs
