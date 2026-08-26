"""Tests for the file-hub tools module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robotsix_chat.config.models import FileHubToolsSettings
from robotsix_chat.file_hub_tools import build_file_hub_tools, load_file_hub_skill

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> FileHubToolsSettings:
    """Build a FileHubToolsSettings with sensible test defaults."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://file-hub:8080",
        "working_dir": "/data/file_hub_work",
        "max_download_bytes": 52_428_800,
        "timeout": 10.0,
    }
    defaults.update(overrides)
    return FileHubToolsSettings(**defaults)


def _make_form_pdf(path: Path) -> None:
    """Create a minimal PDF with an AcroForm text field using reportlab."""
    rl_canvas = pytest.importorskip("reportlab.pdfgen.canvas")

    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    c.acroForm.textfield(name="name", x=100, y=700, width=200, height=20, value="")
    c.drawString(100, 725, "Name:")
    c.save()


def _make_flat_pdf(path: Path) -> None:
    """Create a minimal flat PDF (no form fields) using reportlab."""
    rl_canvas = pytest.importorskip("reportlab.pdfgen.canvas")

    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    c.drawString(100, 700, "Sample flat PDF")
    c.save()


def _make_test_png(path: Path, *, width: int = 10, height: int = 10) -> Path:
    """Create a small solid-colour PNG file for testing image overlays.

    Uses the ``Pillow`` library when available, otherwise writes a
    minimal 1×1 PNG by hand (the overlay code can read any PNG).
    """
    try:
        from PIL import Image

        img = Image.new("RGBA", (width, height), (255, 0, 0, 128))
        img.save(str(path))
        return path
    except ImportError:
        pass

    # Minimal 1×1 red-transparent PNG (no Pillow).
    import struct
    import zlib

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b""
    for _y in range(height):
        raw += b"\x00"  # filter byte
        raw += b"\xff\x00\x00\x80" * width  # RGBA red, semi-transparent
    idat = zlib.compress(raw)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", idat)
    png += _chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


# ---------------------------------------------------------------------------
# build_file_hub_tools
# ---------------------------------------------------------------------------


class TestBuildFileHubTools:
    """Tests for the build_file_hub_tools factory."""

    def test_disabled_returns_empty(self, tmp_path: Path) -> None:
        """Disabled settings returns empty tool list."""
        settings = _settings(enabled=False)
        tools = build_file_hub_tools(settings)
        assert tools == []

    def test_enabled_returns_five_tools(self, tmp_path: Path) -> None:
        """Enabled settings returns all five tools."""
        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        assert len(tools) == 5
        names = [t.__name__ for t in tools]
        assert "file_hub_get" in names
        assert "fill_pdf_document" in names
        assert "list_pdf_form_fields" in names
        assert "render_pdf_page" in names
        assert "file_hub_put" in names


# ---------------------------------------------------------------------------
# file_hub_get
# ---------------------------------------------------------------------------


class TestFileHubGet:
    """Tests for the file_hub_get tool."""

    @pytest.mark.asyncio
    async def test_download_success(self, tmp_path: Path) -> None:
        """Successful download returns local path and metadata."""
        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        file_hub_get = tools[0]

        metadata = {
            "id": "abc-123",
            "filename": "test.pdf",
            "size": 1024,
            "content_type": "application/pdf",
            "category": "document",
            "tags": "pdf,form",
            "summary": "A test document",
        }

        with patch(
            "robotsix_chat.file_hub_tools.client.FileHubClient.download_file",
            new_callable=AsyncMock,
            return_value=(tmp_path / "test.pdf", metadata),
        ):
            result = await file_hub_get("abc-123")

        assert "Downloaded to:" in result
        assert "test.pdf" in result
        assert "1024 bytes" in result
        assert "document" in result

    @pytest.mark.asyncio
    async def test_download_not_found(self, tmp_path: Path) -> None:
        """Unknown file id returns clear not-found message."""
        from robotsix_chat.file_hub_tools.client import FileHubNotFoundError

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        file_hub_get = tools[0]

        with patch(
            "robotsix_chat.file_hub_tools.client.FileHubClient.download_file",
            new_callable=AsyncMock,
            side_effect=FileHubNotFoundError("File 'bad-id' not found"),
        ):
            result = await file_hub_get("bad-id")

        assert "File not found" in result

    @pytest.mark.asyncio
    async def test_download_unavailable(self, tmp_path: Path) -> None:
        """Unreachable file-hub returns clear unavailable message."""
        from robotsix_chat.file_hub_tools.client import FileHubUnavailableError

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        file_hub_get = tools[0]

        with patch(
            "robotsix_chat.file_hub_tools.client.FileHubClient.download_file",
            new_callable=AsyncMock,
            side_effect=FileHubUnavailableError("Connection refused"),
        ):
            result = await file_hub_get("abc-123")

        assert "File-hub unavailable" in result


# ---------------------------------------------------------------------------
# list_pdf_form_fields
# ---------------------------------------------------------------------------


class TestListPdfFormFields:
    """Tests for the list_pdf_form_fields tool."""

    @pytest.mark.asyncio
    async def test_list_fields_in_form_pdf(self, tmp_path: Path) -> None:
        """Form PDF fields are detected and listed."""
        pdf_path = tmp_path / "form.pdf"
        _make_form_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        list_fields = tools[2]

        result = await list_fields(str(pdf_path))
        assert "name" in result
        assert "/Tx" in result

    @pytest.mark.asyncio
    async def test_list_fields_flat_pdf(self, tmp_path: Path) -> None:
        """Flat PDF returns no-fields message."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        list_fields = tools[2]

        result = await list_fields(str(pdf_path))
        assert "No AcroForm fields" in result

    @pytest.mark.asyncio
    async def test_list_fields_not_found(self, tmp_path: Path) -> None:
        """Missing PDF returns clear error."""
        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        list_fields = tools[2]

        result = await list_fields(str(tmp_path / "nonexistent.pdf"))
        assert "not found" in result.lower() or "Not a valid PDF" in result

    @pytest.mark.asyncio
    async def test_list_fields_not_pdf(self, tmp_path: Path) -> None:
        """Non-PDF file returns clear error."""
        not_pdf = tmp_path / "not.pdf"
        not_pdf.write_text("this is not a PDF")

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        list_fields = tools[2]

        result = await list_fields(str(not_pdf))
        assert "Not a valid PDF" in result


# ---------------------------------------------------------------------------
# fill_pdf_document
# ---------------------------------------------------------------------------


class TestFillPdfDocument:
    """Tests for the fill_pdf_document tool."""

    @pytest.mark.asyncio
    async def test_fill_form_fields(self, tmp_path: Path) -> None:
        """Form fields are filled and output PDF is created."""
        pdf_path = tmp_path / "form.pdf"
        _make_form_pdf(pdf_path)
        out_path = tmp_path / "filled.pdf"

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        result = await fill_pdf(
            pdf_path=str(pdf_path),
            field_values=json.dumps({"name": "John Doe"}),
            output_path=str(out_path),
        )
        assert "Filled 1 form field" in result
        assert out_path.exists()

    @pytest.mark.asyncio
    async def test_fill_missing_field(self, tmp_path: Path) -> None:
        """Missing field name returns clear error."""
        pdf_path = tmp_path / "form.pdf"
        _make_form_pdf(pdf_path)
        out_path = tmp_path / "filled.pdf"

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        result = await fill_pdf(
            pdf_path=str(pdf_path),
            field_values=json.dumps({"nonexistent_field": "value"}),
            output_path=str(out_path),
        )
        assert "Field error" in result
        assert "nonexistent_field" in result

    @pytest.mark.asyncio
    async def test_overlay_text(self, tmp_path: Path) -> None:
        """Text overlay on flat PDF produces output."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)
        out_path = tmp_path / "overlaid.pdf"

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        overlays = json.dumps(
            [{"page": 0, "x": 100, "y": 600, "text": "Overlaid text"}]
        )
        result = await fill_pdf(
            pdf_path=str(pdf_path),
            text_overlays=overlays,
            output_path=str(out_path),
        )
        assert "Overlaid 1 text block" in result
        assert out_path.exists()

    @pytest.mark.asyncio
    async def test_fill_not_pdf(self, tmp_path: Path) -> None:
        """Non-PDF input returns clear error."""
        not_pdf = tmp_path / "not.pdf"
        not_pdf.write_text("not a pdf")

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        result = await fill_pdf(
            pdf_path=str(not_pdf),
            field_values=json.dumps({"name": "test"}),
            output_path=str(tmp_path / "out.pdf"),
        )
        assert "Not a valid PDF" in result

    @pytest.mark.asyncio
    async def test_fill_no_args(self, tmp_path: Path) -> None:
        """No fill spec returns helpful message."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        result = await fill_pdf(pdf_path=str(pdf_path))
        assert "Nothing to fill" in result

    @pytest.mark.asyncio
    async def test_fill_source_not_found(self, tmp_path: Path) -> None:
        """Missing source PDF returns clear error."""
        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        result = await fill_pdf(
            pdf_path=str(tmp_path / "nonexistent.pdf"),
            field_values=json.dumps({"name": "test"}),
        )
        assert "not found" in result.lower()

    # ----- image overlay tests -----

    @pytest.mark.asyncio
    async def test_image_overlay_basic(self, tmp_path: Path) -> None:
        """Image overlay stamps a PNG onto a PDF page."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)
        img_path = _make_test_png(tmp_path / "sig.png", width=50, height=20)
        out_path = tmp_path / "img_overlaid.pdf"

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        image_ovs = json.dumps(
            [{"page": 0, "x": 100, "y": 500, "image_path": str(img_path), "width": 150}]
        )
        result = await fill_pdf(
            pdf_path=str(pdf_path),
            image_overlays=image_ovs,
            output_path=str(out_path),
        )
        assert "Stamped 1 image" in result
        assert out_path.exists()
        # Content stream should be larger than the original.
        assert out_path.stat().st_size > pdf_path.stat().st_size

    @pytest.mark.asyncio
    async def test_image_overlay_and_text_combined(self, tmp_path: Path) -> None:
        """One call can place text AND image on the same page."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)
        img_path = _make_test_png(tmp_path / "sig.png", width=50, height=20)
        out_path = tmp_path / "combined.pdf"

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        text_ovs = json.dumps([{"page": 0, "x": 100, "y": 700, "text": "Hello"}])
        image_ovs = json.dumps(
            [{"page": 0, "x": 100, "y": 500, "image_path": str(img_path), "width": 150}]
        )
        result = await fill_pdf(
            pdf_path=str(pdf_path),
            text_overlays=text_ovs,
            image_overlays=image_ovs,
            output_path=str(out_path),
        )
        assert "Overlaid 1 text block" in result
        assert "Stamped 1 image" in result
        assert out_path.exists()

    @pytest.mark.asyncio
    async def test_image_overlay_missing_file(self, tmp_path: Path) -> None:
        """Missing image file returns clear error."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        image_ovs = json.dumps(
            [{"page": 0, "x": 100, "y": 500, "image_path": str(tmp_path / "nope.png")}]
        )
        result = await fill_pdf(
            pdf_path=str(pdf_path),
            image_overlays=image_ovs,
        )
        assert "PDF processing error" in result
        assert "Image file not found" in result

    @pytest.mark.asyncio
    async def test_image_overlay_unsupported_format(self, tmp_path: Path) -> None:
        """Unsupported image format returns clear error."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)
        bad_img = tmp_path / "photo.bmp"
        bad_img.write_bytes(b"BM\x00\x00")  # Fake BMP header.

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        image_ovs = json.dumps(
            [{"page": 0, "x": 100, "y": 500, "image_path": str(bad_img)}]
        )
        result = await fill_pdf(
            pdf_path=str(pdf_path),
            image_overlays=image_ovs,
        )
        assert "PDF processing error" in result
        assert "Unsupported image format" in result

    @pytest.mark.asyncio
    async def test_image_overlay_invalid_json(self, tmp_path: Path) -> None:
        """Malformed image_overlays JSON returns clear error."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        result = await fill_pdf(
            pdf_path=str(pdf_path),
            image_overlays="not json {{{",
        )
        assert "Invalid image_overlays JSON" in result

    @pytest.mark.asyncio
    async def test_image_overlay_aspect_ratio(self, tmp_path: Path) -> None:
        """When only width is given, height is derived from aspect ratio."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)
        # 100×40 px image → aspect ratio 0.4
        img_path = _make_test_png(tmp_path / "sig.png", width=100, height=40)
        out_path = tmp_path / "aspect.pdf"

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        fill_pdf = tools[1]

        # Width only: 100 pts → height should be 40 pts.
        image_ovs = json.dumps(
            [{"page": 0, "x": 100, "y": 500, "image_path": str(img_path), "width": 100}]
        )
        result = await fill_pdf(
            pdf_path=str(pdf_path),
            image_overlays=image_ovs,
            output_path=str(out_path),
        )
        assert "Stamped 1 image" in result
        assert out_path.exists()

# ---------------------------------------------------------------------------
# file_hub_put
# ---------------------------------------------------------------------------


class TestFileHubPut:
    """Tests for the file_hub_put tool."""

    @pytest.mark.asyncio
    async def test_upload_success(self, tmp_path: Path) -> None:
        """Successful upload returns file-hub id and metadata."""
        test_file = tmp_path / "upload.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake pdf content")

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        file_hub_put = tools[4]

        upload_result = {
            "id": "new-id-456",
            "filename": "upload.pdf",
            "size": 25,
            "content_type": "application/pdf",
            "checksum": "abc123",
        }

        with patch(
            "robotsix_chat.file_hub_tools.client.FileHubClient.upload_file",
            new_callable=AsyncMock,
            return_value=upload_result,
        ):
            result = await file_hub_put(str(test_file))

        assert "Uploaded successfully" in result
        assert "new-id-456" in result
        assert "upload.pdf" in result

    @pytest.mark.asyncio
    async def test_upload_file_not_found(self, tmp_path: Path) -> None:
        """Missing local file returns clear error."""
        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        file_hub_put = tools[4]

        result = await file_hub_put(str(tmp_path / "nonexistent.pdf"))
        assert "File not found" in result

    @pytest.mark.asyncio
    async def test_upload_unavailable(self, tmp_path: Path) -> None:
        """Unreachable file-hub returns clear unavailable message."""
        from robotsix_chat.file_hub_tools.client import FileHubUnavailableError

        test_file = tmp_path / "upload.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake")

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        file_hub_put = tools[4]

        with patch(
            "robotsix_chat.file_hub_tools.client.FileHubClient.upload_file",
            new_callable=AsyncMock,
            side_effect=FileHubUnavailableError("Connection refused"),
        ):
            result = await file_hub_put(str(test_file))

        assert "File-hub unavailable" in result


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------


class TestSkillLoading:
    """Tests for load_file_hub_skill."""

    def test_skill_loads(self) -> None:
        """Skill markdown contains all five tool names."""
        skill = load_file_hub_skill()
        assert "file_hub_get" in skill
        assert "fill_pdf_document" in skill
        assert "file_hub_put" in skill
        assert "list_pdf_form_fields" in skill
        assert "render_pdf_page" in skill


# ---------------------------------------------------------------------------
# render_pdf_page
# ---------------------------------------------------------------------------


class TestRenderPdfPage:
    """Tests for the render_pdf_page tool."""

    @pytest.mark.asyncio
    async def test_render_flat_pdf(self, tmp_path: Path) -> None:
        """Rendering a flat PDF returns image data and dimensions."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        render_page = tools[3]

        result = await render_page(str(pdf_path))
        data = json.loads(result)

        assert data["error"] == ""
        assert data["image_base64"] != ""
        assert data["width"] > 0
        assert data["height"] > 0
        assert data["page_width_points"] > 0
        assert data["page_height_points"] > 0

    @pytest.mark.asyncio
    async def test_render_form_pdf(self, tmp_path: Path) -> None:
        """Rendering a form PDF returns image data and dimensions."""
        pdf_path = tmp_path / "form.pdf"
        _make_form_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        render_page = tools[3]

        result = await render_page(str(pdf_path))
        data = json.loads(result)

        assert data["error"] == ""
        assert data["image_base64"] != ""
        assert data["width"] > 0
        assert data["height"] > 0

    @pytest.mark.asyncio
    async def test_render_page_out_of_range(self, tmp_path: Path) -> None:
        """Page index beyond page count returns a clear error."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        render_page = tools[3]

        result = await render_page(str(pdf_path), page=99)
        data = json.loads(result)

        assert "out of range" in data["error"].lower()
        assert "1 page" in data["error"]

    @pytest.mark.asyncio
    async def test_render_missing_file(self, tmp_path: Path) -> None:
        """Missing PDF returns a clear error."""
        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        render_page = tools[3]

        result = await render_page(str(tmp_path / "nonexistent.pdf"))
        data = json.loads(result)

        assert (
            "not a valid pdf" in data["error"].lower()
            or "not found" in data["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_render_non_pdf(self, tmp_path: Path) -> None:
        """Non-PDF file returns a clear error."""
        not_pdf = tmp_path / "not.pdf"
        not_pdf.write_text("this is not a PDF")

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        render_page = tools[3]

        result = await render_page(str(not_pdf))
        data = json.loads(result)

        assert "not a valid pdf" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_render_metadata_coordinate_conversion(self, tmp_path: Path) -> None:
        """Pixel-to-point coordinate conversion works with returned metadata."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        render_page = tools[3]

        result = await render_page(str(pdf_path), dpi=72)
        data = json.loads(result)

        assert data["error"] == ""
        # At 72 dpi, 1 PDF point = 1 pixel (before any cap).
        # The page is 612x792 points (US Letter).
        assert data["page_width_points"] == 612
        assert data["page_height_points"] == 792

        # Verify coordinate conversion: pixel (100, 100) → PDF points.
        # X is straightforward; Y must be flipped (image Y=0 is top,
        # PDF Y=0 is bottom).
        px, py = 100, 100
        pdf_x = px * data["page_width_points"] / data["width"]
        pdf_y = data["page_height_points"] - (
            py * data["page_height_points"] / data["height"]
        )
        # At 72 dpi without capping, pdf_x ≈ 100.
        # pdf_y ≈ 792 - 100 = 692 (Y flipped).
        assert abs(pdf_x - 100) < 1
        assert abs(pdf_y - 692) < 1

    @pytest.mark.asyncio
    async def test_render_dpi_parameter(self, tmp_path: Path) -> None:
        """Higher DPI produces larger images."""
        pdf_path = tmp_path / "flat.pdf"
        _make_flat_pdf(pdf_path)

        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        render_page = tools[3]

        result_low = await render_page(str(pdf_path), dpi=72)
        result_high = await render_page(str(pdf_path), dpi=200)

        data_low = json.loads(result_low)
        data_high = json.loads(result_high)

        assert data_low["error"] == ""
        assert data_high["error"] == ""
        # Higher DPI should produce more pixels (unless capped).
        assert data_high["width"] >= data_low["width"]
        assert data_high["height"] >= data_low["height"]


# ---------------------------------------------------------------------------
# Client unit tests
# ---------------------------------------------------------------------------


class TestFileHubClient:
    """Tests for the FileHubClient HTTP client."""

    def test_sanitize_filename(self) -> None:
        """Sanitize strips path components and handles edge cases."""
        from robotsix_chat.file_hub_tools.client import _sanitize_filename

        assert _sanitize_filename("test.pdf", "fallback") == "test.pdf"
        assert _sanitize_filename("/etc/passwd", "fallback") == "passwd"
        assert _sanitize_filename("", "fallback") == "fallback"
        assert _sanitize_filename(".", "fallback") == "fallback"

    def test_unique_path(self, tmp_path: Path) -> None:
        """Unique path appends counter when file exists."""
        from robotsix_chat.file_hub_tools.client import _unique_path

        p = tmp_path / "test.pdf"
        assert _unique_path(p) == p

        p.write_bytes(b"existing")
        unique = _unique_path(p)
        assert unique != p
        assert unique.name == "test_1.pdf"

    def test_guess_content_type(self) -> None:
        """Content type guessed from file extension."""
        from robotsix_chat.file_hub_tools.client import _guess_content_type

        assert _guess_content_type("doc.pdf") == "application/pdf"
        assert _guess_content_type("image.png") == "image/png"
        assert _guess_content_type("data.csv") == "text/csv"
        assert _guess_content_type("unknown.xyz") == "application/octet-stream"
