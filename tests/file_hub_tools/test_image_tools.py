"""Tests for the generic image-transform tool and its pure helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robotsix_chat.config.models import FileHubToolsSettings
from robotsix_chat.file_hub_tools import build_file_hub_tools
from robotsix_chat.file_hub_tools.image_tools import (
    MAX_INPUT_BYTES,
    ImageError,
    ImageNotDecodableError,
    ImageTooLargeError,
    transform_image,
)

PIL_Image = pytest.importorskip("PIL.Image")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> FileHubToolsSettings:
    defaults: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://file-hub:8080",
        "working_dir": "/data/file_hub_work",
        "max_download_bytes": 52_428_800,
        "timeout": 10.0,
    }
    defaults.update(overrides)
    return FileHubToolsSettings(**defaults)


def _make_jpeg(path: Path, *, width: int = 4000, height: int = 3000) -> Path:
    """Write a JPEG with EXIF metadata for testing."""
    from PIL import Image

    img = Image.new("RGB", (width, height), (120, 60, 30))
    exif = Image.Exif()
    exif[0x010E] = "test-description"  # ImageDescription tag
    img.save(str(path), format="JPEG", quality=95, exif=exif.tobytes())
    return path


def _make_png(path: Path, *, width: int = 200, height: int = 100) -> Path:
    from PIL import Image

    img = Image.new("RGBA", (width, height), (0, 128, 255, 200))
    img.save(str(path), format="PNG")
    return path


def _get_tool(tools: list[Any], name: str) -> Any:
    for tool in tools:
        if tool.__name__ == name:
            return tool
    raise AssertionError(f"tool {name!r} not found")


# ---------------------------------------------------------------------------
# Pure transform_image helper
# ---------------------------------------------------------------------------


class TestTransformImageHelper:
    """Tests for the pure ``transform_image`` function."""

    def test_resize_and_compress(self, tmp_path: Path) -> None:
        """Resize downscales, compresses, and strips EXIF by default."""
        src = _make_jpeg(tmp_path / "photo.jpg")
        result = transform_image(
            src,
            {"resize": {"max_width": 1600}, "quality": 75},
            tmp_path / "out",
        )

        assert result["output_format"] == "JPEG"
        assert result["output_width"] == 1600
        # Aspect ratio preserved (4000x3000 -> 1600x1200).
        assert result["output_height"] == 1200
        assert result["output_bytes"] < result["input_bytes"]

        from PIL import Image

        with Image.open(result["output_path"]) as out:
            assert out.format == "JPEG"
            # EXIF stripped by default.
            assert not out.getexif()

    def test_never_upscale(self, tmp_path: Path) -> None:
        """A max dimension larger than the source leaves size unchanged."""
        src = _make_png(tmp_path / "small.png", width=200, height=100)
        result = transform_image(
            src,
            {"resize": {"max_width": 5000}},
            tmp_path / "out",
        )
        assert result["output_width"] == 200
        assert result["output_height"] == 100

    def test_format_conversion(self, tmp_path: Path) -> None:
        """Explicit format converts PNG to WebP."""
        src = _make_png(tmp_path / "logo.png")
        result = transform_image(
            src,
            {"format": "webp"},
            tmp_path / "out",
        )
        assert result["output_format"] == "WEBP"
        assert result["output_path"].suffix == ".webp"

        from PIL import Image

        with Image.open(result["output_path"]) as out:
            assert out.format == "WEBP"

    def test_preserve_metadata_when_requested(self, tmp_path: Path) -> None:
        """strip_metadata=False keeps EXIF on the output."""
        src = _make_jpeg(tmp_path / "photo.jpg", width=800, height=600)
        result = transform_image(
            src,
            {"strip_metadata": False},
            tmp_path / "out",
        )

        from PIL import Image

        with Image.open(result["output_path"]) as out:
            assert out.getexif()

    def test_oversize_rejection(self, tmp_path: Path) -> None:
        """An input larger than MAX_INPUT_BYTES raises ImageTooLargeError."""
        src = tmp_path / "big.bin"
        src.write_bytes(b"\x00" * (MAX_INPUT_BYTES + 1))
        with pytest.raises(ImageTooLargeError):
            transform_image(src, {}, tmp_path / "out")

    def test_non_image_rejection(self, tmp_path: Path) -> None:
        """A non-image file raises ImageNotDecodableError."""
        src = tmp_path / "notes.txt"
        src.write_bytes(b"this is not an image")
        with pytest.raises(ImageNotDecodableError):
            transform_image(src, {}, tmp_path / "out")

    def test_bad_quality_rejected(self, tmp_path: Path) -> None:
        """Out-of-range quality raises ImageError."""
        src = _make_png(tmp_path / "logo.png")
        with pytest.raises(ImageError):
            transform_image(src, {"quality": 500}, tmp_path / "out")

    def test_unknown_format_rejected(self, tmp_path: Path) -> None:
        """An unsupported output format raises ImageError."""
        src = _make_png(tmp_path / "logo.png")
        with pytest.raises(ImageError):
            transform_image(src, {"format": "gif"}, tmp_path / "out")


# ---------------------------------------------------------------------------
# transform_image tool (file-hub round-trip)
# ---------------------------------------------------------------------------


class TestTransformImageTool:
    """Tests for the ``transform_image`` MCP tool."""

    @pytest.mark.asyncio
    async def test_resize_compress_roundtrip(self, tmp_path: Path) -> None:
        """A successful call downloads, transforms, uploads, returns new id."""
        settings = _settings(working_dir=str(tmp_path))
        tool = _get_tool(build_file_hub_tools(settings), "transform_image")

        src = _make_jpeg(tmp_path / "photo.jpg")

        with (
            patch(
                "robotsix_chat.file_hub_tools.client.FileHubClient.download_file",
                new_callable=AsyncMock,
                return_value=(src, {"filename": "photo.jpg"}),
            ),
            patch(
                "robotsix_chat.file_hub_tools.client.FileHubClient.upload_file",
                new_callable=AsyncMock,
                return_value={"id": "new-id-123", "filename": "photo.jpg"},
            ) as upload,
        ):
            result = await tool(
                "src-id", operations='{"resize": {"max_width": 1600}, "quality": 75}'
            )

        assert "New file-hub ID: new-id-123" in result
        assert "1600x1200" in result
        # The uploaded file is the transformed output, not the source.
        uploaded_path = upload.call_args.args[0]
        assert Path(uploaded_path) != src

    @pytest.mark.asyncio
    async def test_format_conversion_roundtrip(self, tmp_path: Path) -> None:
        """Format conversion produces a WebP upload."""
        settings = _settings(working_dir=str(tmp_path))
        tool = _get_tool(build_file_hub_tools(settings), "transform_image")

        src = _make_png(tmp_path / "logo.png")

        with (
            patch(
                "robotsix_chat.file_hub_tools.client.FileHubClient.download_file",
                new_callable=AsyncMock,
                return_value=(src, {"filename": "logo.png"}),
            ),
            patch(
                "robotsix_chat.file_hub_tools.client.FileHubClient.upload_file",
                new_callable=AsyncMock,
                return_value={"id": "webp-id", "filename": "logo.webp"},
            ) as upload,
        ):
            result = await tool("src-id", operations='{"format": "webp"}')

        assert "Output format: WEBP" in result
        uploaded_path = Path(upload.call_args.args[0])
        assert uploaded_path.suffix == ".webp"

    @pytest.mark.asyncio
    async def test_missing_file_error(self, tmp_path: Path) -> None:
        """A non-existent file-hub id returns a clear error, not a crash."""
        from robotsix_chat.file_hub_tools.client import FileHubNotFoundError

        settings = _settings(working_dir=str(tmp_path))
        tool = _get_tool(build_file_hub_tools(settings), "transform_image")

        with patch(
            "robotsix_chat.file_hub_tools.client.FileHubClient.download_file",
            new_callable=AsyncMock,
            side_effect=FileHubNotFoundError("File 'bad-id' not found"),
        ):
            result = await tool("bad-id")

        assert "File not found" in result

    @pytest.mark.asyncio
    async def test_non_image_error(self, tmp_path: Path) -> None:
        """A non-image object returns a clear decode error, not a crash."""
        settings = _settings(working_dir=str(tmp_path))
        tool = _get_tool(build_file_hub_tools(settings), "transform_image")

        not_image = tmp_path / "notes.txt"
        not_image.write_bytes(b"definitely not an image")

        with patch(
            "robotsix_chat.file_hub_tools.client.FileHubClient.download_file",
            new_callable=AsyncMock,
            return_value=(not_image, {"filename": "notes.txt"}),
        ):
            result = await tool("txt-id")

        assert "Not a decodable image" in result

    @pytest.mark.asyncio
    async def test_invalid_operations_json(self, tmp_path: Path) -> None:
        """Malformed operations JSON returns a clear error."""
        settings = _settings(working_dir=str(tmp_path))
        tool = _get_tool(build_file_hub_tools(settings), "transform_image")

        result = await tool("src-id", operations="{not json}")
        assert "Invalid operations JSON" in result
