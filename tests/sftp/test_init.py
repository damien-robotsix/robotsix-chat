"""Tests for the build_sftp_tools factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from robotsix_chat.config.models import SftpSettings
from robotsix_chat.sftp import build_sftp_tools


class TestBuildSftpToolsDisabled:
    """Tests for build_sftp_tools when SFTP is disabled."""

    def test_disabled_returns_empty_list(self) -> None:
        """When settings.enabled is False, an empty list is returned."""
        settings = SftpSettings(
            enabled=False,
            host="test.example.com",
            username="tester",
            password=SecretStr("secret"),
        )
        result = build_sftp_tools(settings)
        assert result == []


class TestBuildSftpToolsEnabled:
    """Tests for build_sftp_tools tool wiring when SFTP is enabled."""

    @pytest.fixture(autouse=True)
    def _mock_client(self, monkeypatch) -> None:
        from robotsix_chat.sftp import client as client_module
        from robotsix_chat.sftp.client import SftpError, SftpPathError

        self.mock_client_class = MagicMock()
        self.mock_client_instance = MagicMock()
        self.mock_client_class.return_value = self.mock_client_instance

        self.mock_client_instance.read_file = AsyncMock(return_value="file content")
        self.mock_client_instance.write_file = AsyncMock(
            return_value="Wrote 5 bytes to /srv/config.txt"
        )
        self.mock_client_instance.list_directory = AsyncMock(
            return_value="file1\nfile2"
        )
        self.mock_client_instance.file_exists = AsyncMock(return_value=True)

        monkeypatch.setattr(client_module, "SftpClient", self.mock_client_class)
        monkeypatch.setattr(client_module, "SftpError", SftpError)
        monkeypatch.setattr(client_module, "SftpPathError", SftpPathError)

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

    def test_returns_four_tools(self) -> None:
        """build_sftp_tools returns a list of 4 callables."""
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        assert len(tools) == 4
        for tool in tools:
            assert callable(tool)

    def test_tools_have_correct_names(self) -> None:
        """Each tool has the correct __name__."""
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        names = [t.__name__ for t in tools]
        assert names == [
            "sftp_read_file",
            "sftp_write_file",
            "sftp_list_directory",
            "sftp_file_exists",
        ]

    @pytest.mark.asyncio
    async def test_read_file_success(self) -> None:
        """sftp_read_file returns content on success."""
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        read_fn = tools[0]
        result = await read_fn("config.txt")
        assert result == "file content"
        self.mock_client_instance.read_file.assert_called_once_with("config.txt")

    @pytest.mark.asyncio
    async def test_write_file_success(self) -> None:
        """sftp_write_file returns success message."""
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        write_fn = tools[1]
        result = await write_fn("config.txt", "hello")
        assert result == "Wrote 5 bytes to /srv/config.txt"
        self.mock_client_instance.write_file.assert_called_once_with(
            "config.txt", "hello", backup=True
        )

    @pytest.mark.asyncio
    async def test_list_directory_success(self) -> None:
        """sftp_list_directory returns directory listing."""
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        list_fn = tools[2]
        result = await list_fn("/srv")
        assert result == "file1\nfile2"
        self.mock_client_instance.list_directory.assert_called_once_with("/srv")

    @pytest.mark.asyncio
    async def test_list_directory_empty(self) -> None:
        """sftp_list_directory returns empty-directory message when empty."""
        self.mock_client_instance.list_directory = AsyncMock(return_value="")
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        list_fn = tools[2]
        result = await list_fn("/srv")
        assert result == "(empty directory: '/srv')"

    @pytest.mark.asyncio
    async def test_file_exists_true(self) -> None:
        """sftp_file_exists returns 'true' when path exists."""
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        exists_fn = tools[3]
        result = await exists_fn("/srv/config.txt")
        assert result == "true"
        self.mock_client_instance.file_exists.assert_called_once_with(
            "/srv/config.txt"
        )

    @pytest.mark.asyncio
    async def test_file_exists_false(self) -> None:
        """sftp_file_exists returns 'false' when path does not exist."""
        self.mock_client_instance.file_exists = AsyncMock(return_value=False)
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        exists_fn = tools[3]
        result = await exists_fn("/srv/missing.txt")
        assert result == "false"


class TestSftpErrorTranslation:
    """Tests for SftpError / SftpPathError translation in closures."""

    @pytest.fixture(autouse=True)
    def _mock_client(self, monkeypatch) -> None:
        from robotsix_chat.sftp import client as client_module
        from robotsix_chat.sftp.client import SftpError, SftpPathError

        self.mock_client_class = MagicMock()
        self.mock_client_instance = MagicMock()
        self.mock_client_class.return_value = self.mock_client_instance

        self.mock_client_instance.read_file = AsyncMock()
        self.mock_client_instance.write_file = AsyncMock()
        self.mock_client_instance.list_directory = AsyncMock()
        self.mock_client_instance.file_exists = AsyncMock()

        monkeypatch.setattr(client_module, "SftpClient", self.mock_client_class)
        monkeypatch.setattr(client_module, "SftpError", SftpError)
        monkeypatch.setattr(client_module, "SftpPathError", SftpPathError)

    @staticmethod
    def _make_settings() -> SftpSettings:
        return SftpSettings(
            enabled=True,
            host="test.example.com",
            username="tester",
            password=SecretStr("secret"),
            remote_root="/srv",
        )

    @pytest.mark.asyncio
    async def test_read_file_sftp_error_returns_message(self) -> None:
        """sftp_read_file returns error message on SftpError."""
        from robotsix_chat.sftp.client import SftpError

        self.mock_client_instance.read_file = AsyncMock(
            side_effect=SftpError("connection refused")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        read_fn = tools[0]
        result = await read_fn("config.txt")
        assert result == "SFTP read error: connection refused"

    @pytest.mark.asyncio
    async def test_read_file_sftp_path_error_returns_message(self) -> None:
        """sftp_read_file returns error message on SftpPathError."""
        from robotsix_chat.sftp.client import SftpPathError

        self.mock_client_instance.read_file = AsyncMock(
            side_effect=SftpPathError("escapes remote_root")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        read_fn = tools[0]
        result = await read_fn("../../../etc/passwd")
        assert result == "SFTP read error: escapes remote_root"

    @pytest.mark.asyncio
    async def test_write_file_sftp_error_returns_message(self) -> None:
        """sftp_write_file returns error message on SftpError."""
        from robotsix_chat.sftp.client import SftpError

        self.mock_client_instance.write_file = AsyncMock(
            side_effect=SftpError("permission denied")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        write_fn = tools[1]
        result = await write_fn("config.txt", "content")
        assert result == "SFTP write error: permission denied"

    @pytest.mark.asyncio
    async def test_list_directory_sftp_error_returns_message(self) -> None:
        """sftp_list_directory returns error message on SftpError."""
        from robotsix_chat.sftp.client import SftpError

        self.mock_client_instance.list_directory = AsyncMock(
            side_effect=SftpError("not a directory")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        list_fn = tools[2]
        result = await list_fn("/srv")
        assert result == "SFTP list error: not a directory"

    @pytest.mark.asyncio
    async def test_file_exists_sftp_error_returns_message(self) -> None:
        """sftp_file_exists returns error message on SftpError."""
        from robotsix_chat.sftp.client import SftpError

        self.mock_client_instance.file_exists = AsyncMock(
            side_effect=SftpError("connection timeout")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        exists_fn = tools[3]
        result = await exists_fn("/srv/config.txt")
        assert result == "SFTP exists check failed: connection timeout"


class TestImportErrorFallback:
    """Tests for ImportError fallback in closures."""

    @pytest.fixture(autouse=True)
    def _mock_client(self, monkeypatch) -> None:
        from robotsix_chat.sftp import client as client_module
        from robotsix_chat.sftp.client import SftpError, SftpPathError

        self.mock_client_class = MagicMock()
        self.mock_client_instance = MagicMock()
        self.mock_client_class.return_value = self.mock_client_instance

        self.mock_client_instance.read_file = AsyncMock()
        self.mock_client_instance.write_file = AsyncMock()
        self.mock_client_instance.list_directory = AsyncMock()
        self.mock_client_instance.file_exists = AsyncMock()

        monkeypatch.setattr(client_module, "SftpClient", self.mock_client_class)
        monkeypatch.setattr(client_module, "SftpError", SftpError)
        monkeypatch.setattr(client_module, "SftpPathError", SftpPathError)

    @staticmethod
    def _make_settings() -> SftpSettings:
        return SftpSettings(
            enabled=True,
            host="test.example.com",
            username="tester",
            password=SecretStr("secret"),
            remote_root="/srv",
        )

    INSTALL_HINT = (
        "SFTP tools require the ``asyncssh`` package. "
        "Install it with: pip install asyncssh"
    )

    @pytest.mark.asyncio
    async def test_read_file_import_error_returns_install_hint(self) -> None:
        """sftp_read_file returns install hint on ImportError."""
        self.mock_client_instance.read_file = AsyncMock(
            side_effect=ImportError("No module named 'asyncssh'")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        read_fn = tools[0]
        result = await read_fn("config.txt")
        assert result == self.INSTALL_HINT

    @pytest.mark.asyncio
    async def test_write_file_import_error_returns_install_hint(self) -> None:
        """sftp_write_file returns install hint on ImportError."""
        self.mock_client_instance.write_file = AsyncMock(
            side_effect=ImportError("No module named 'asyncssh'")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        write_fn = tools[1]
        result = await write_fn("config.txt", "content")
        assert result == self.INSTALL_HINT

    @pytest.mark.asyncio
    async def test_list_directory_import_error_returns_install_hint(self) -> None:
        """sftp_list_directory returns install hint on ImportError."""
        self.mock_client_instance.list_directory = AsyncMock(
            side_effect=ImportError("No module named 'asyncssh'")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        list_fn = tools[2]
        result = await list_fn("/srv")
        assert result == self.INSTALL_HINT

    @pytest.mark.asyncio
    async def test_file_exists_import_error_returns_install_hint(self) -> None:
        """sftp_file_exists returns install hint on ImportError."""
        self.mock_client_instance.file_exists = AsyncMock(
            side_effect=ImportError("No module named 'asyncssh'")
        )
        settings = self._make_settings()
        tools = build_sftp_tools(settings)
        exists_fn = tools[3]
        result = await exists_fn("/srv/config.txt")
        assert result == self.INSTALL_HINT
