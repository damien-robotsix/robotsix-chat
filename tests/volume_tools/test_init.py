"""Tests for robotsix_chat.volume_tools."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from robotsix_chat.config.models import VolumeToolsSettings
from robotsix_chat.volume_tools import build_volume_tools, load_volume_tools_skill


def _settings(**overrides) -> VolumeToolsSettings:
    """Build VolumeToolsSettings with *overrides* on top of defaults."""
    defaults: dict[str, object] = {"enabled": True, "root_path": "/data"}
    defaults.update(overrides)
    return VolumeToolsSettings.model_validate(defaults)


class TestBuildVolumeToolsDisabled:
    """Tests for build_volume_tools when volume_tools is disabled."""

    def test_disabled_returns_empty_list(self) -> None:
        """When settings.enabled is False, an empty list is returned."""
        settings = _settings(enabled=False)
        result = build_volume_tools(settings)
        assert result == []


class TestBuildVolumeToolsEnabled:
    """Tests for build_volume_tools tool wiring when enabled."""

    def test_enabled_returns_list_volume_files(self) -> None:
        """When enabled, a single list_volume_files callable is returned."""
        settings = _settings()
        result = build_volume_tools(settings)
        assert len(result) == 1
        tool = result[0]
        assert callable(tool)
        assert tool.__name__ == "list_volume_files"


class TestListVolumeFiles:
    """Integration tests for the list_volume_files tool."""

    @pytest.mark.asyncio
    async def test_root_listing(self) -> None:
        """Listing the root (empty path) returns its entries."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "file_a.txt").write_text("hello")
            (Path(tmp) / "subdir").mkdir()
            settings = _settings(root_path=tmp)
            tools = build_volume_tools(settings)
            tool = tools[0]
            result = await tool("")
            assert "[DIR]  subdir/" in result
            assert "[FILE] file_a.txt" in result

    @pytest.mark.asyncio
    async def test_subdirectory_listing(self) -> None:
        """Listing a subdirectory returns only its entries."""
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "nested"
            sub.mkdir()
            (sub / "inner.txt").write_text("x")
            settings = _settings(root_path=tmp)
            tools = build_volume_tools(settings)
            tool = tools[0]
            result = await tool("nested")
            assert "[FILE] inner.txt" in result

    @pytest.mark.asyncio
    async def test_empty_directory(self) -> None:
        """An empty directory returns a descriptive message."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "empty_sub").mkdir()
            settings = _settings(root_path=tmp)
            tools = build_volume_tools(settings)
            tool = tools[0]
            result = await tool("empty_sub")
            assert "empty directory" in result

    @pytest.mark.asyncio
    async def test_not_a_directory(self) -> None:
        """Passing a file path returns an error message."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "plain.txt").write_text("hi")
            settings = _settings(root_path=tmp)
            tools = build_volume_tools(settings)
            tool = tools[0]
            result = await tool("plain.txt")
            assert "Not a directory" in result

    @pytest.mark.asyncio
    async def test_nonexistent_path(self) -> None:
        """Passing a nonexistent path returns an error message."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(root_path=tmp)
            tools = build_volume_tools(settings)
            tool = tools[0]
            result = await tool("nonexistent")
            assert "does not exist" in result

    @pytest.mark.asyncio
    async def test_path_escape_rejected(self) -> None:
        """A path that escapes the root is rejected with an access-denied message."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(root_path=tmp)
            tools = build_volume_tools(settings)
            tool = tools[0]
            result = await tool("../etc")
            assert "access denied" in result.lower()

    @pytest.mark.asyncio
    async def test_symlink_to_outside_not_followed(self) -> None:
        """A symlink to outside the root appears as its own entry, not followed."""
        with tempfile.TemporaryDirectory() as tmp:
            outside = tempfile.mkdtemp()
            try:
                (Path(tmp) / "link").symlink_to(outside)
                settings = _settings(root_path=tmp)
                tools = build_volume_tools(settings)
                tool = tools[0]
                result = await tool("")
                # The symlink itself should appear (it's a symlink, not dir/file)
                assert "link" in result
            finally:
                Path(outside).rmdir()


class TestLoadVolumeToolsSkill:
    """Tests for load_volume_tools_skill."""

    def test_skill_loads_non_empty(self) -> None:
        """The skill.md file loads and mentions list_volume_files."""
        skill = load_volume_tools_skill()
        assert skill != ""
        assert "list_volume_files" in skill
