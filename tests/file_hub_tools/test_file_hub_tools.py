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
    try:
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        pytest.skip("reportlab not installed")

    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    c.acroForm.textfield(name="name", x=100, y=700, width=200, height=20, value="")
    c.drawString(100, 725, "Name:")
    c.save()


def _make_flat_pdf(path: Path) -> None:
    """Create a minimal flat PDF (no form fields) using reportlab."""
    try:
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        pytest.skip("reportlab not installed")

    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    c.drawString(100, 700, "Sample flat PDF")
    c.save()


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

    def test_enabled_returns_four_tools(self, tmp_path: Path) -> None:
        """Enabled settings returns all four tools."""
        settings = _settings(working_dir=str(tmp_path))
        tools = build_file_hub_tools(settings)
        assert len(tools) == 4
        names = [t.__name__ for t in tools]
        assert "file_hub_get" in names
        assert "fill_pdf_document" in names
        assert "list_pdf_form_fields" in names
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
        file_hub_put = tools[3]

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
        file_hub_put = tools[3]

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
        file_hub_put = tools[3]

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
        """Skill markdown contains all four tool names."""
        skill = load_file_hub_skill()
        assert "file_hub_get" in skill
        assert "fill_pdf_document" in skill
        assert "file_hub_put" in skill
        assert "list_pdf_form_fields" in skill


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
