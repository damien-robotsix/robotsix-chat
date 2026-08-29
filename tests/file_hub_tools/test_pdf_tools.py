"""Unit tests for :mod:`robotsix_chat.file_hub_tools.pdf_tools`.

Focuses on the pure geometry/validation helpers (``_require_pdf``,
``_image_aspect_ratio``, ``_image_native_dimensions``) and the pypdf-only
paths of ``list_form_fields`` / ``fill_form_fields``.  These do not require
``reportlab``, ``pypdfium2`` or ``Pillow`` to be installed — the image
helpers exercise the stdlib PNG-IHDR fallback when Pillow is absent, and
the tests build fixture PDFs/PNGs in memory so they run anywhere ``pypdf``
is available.
"""

from __future__ import annotations

import importlib.util
import struct
import zlib
from pathlib import Path

import pytest

from robotsix_chat.file_hub_tools.pdf_tools import (
    PdfFieldNotFoundError,
    PdfNotPdfError,
    _image_aspect_ratio,
    _image_native_dimensions,
    _require_pdf,
    fill_form_fields,
    list_form_fields,
)

_PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None


def _make_png(path: Path, width: int, height: int) -> Path:
    """Write a minimal but valid truecolour PNG of the given dimensions."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    # bit depth 8, colour type 2 (truecolour), no interlace.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = (b"\x00" + b"\x00\x00\x00" * width) * height
    idat = zlib.compress(raw)
    path.write_bytes(
        signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )
    return path


def _make_blank_pdf(path: Path) -> Path:
    """Write a valid, form-less PDF using pypdf (no reportlab needed)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


# ---------------------------------------------------------------------------
# _require_pdf
# ---------------------------------------------------------------------------


class TestRequirePdf:
    """Validation branches of the ``_require_pdf`` guard."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A non-existent path raises with a 'not found' message."""
        with pytest.raises(PdfNotPdfError, match="not found"):
            _require_pdf(tmp_path / "missing.pdf")

    def test_non_pdf_file_raises(self, tmp_path: Path) -> None:
        """A file lacking the %PDF header raises."""
        bad = tmp_path / "note.txt"
        bad.write_text("this is not a PDF")
        with pytest.raises(PdfNotPdfError, match="%PDF"):
            _require_pdf(bad)

    def test_valid_pdf_header_passes(self, tmp_path: Path) -> None:
        """A file with a valid %PDF header passes without raising."""
        good = tmp_path / "ok.pdf"
        good.write_bytes(b"%PDF-1.7\nrest of file")
        # Returns None and does not raise for a valid header.
        _require_pdf(good)


# ---------------------------------------------------------------------------
# _image_aspect_ratio
# ---------------------------------------------------------------------------


class TestImageAspectRatio:
    """Aspect-ratio computation and its terminal error branch."""

    def test_returns_height_over_width(self, tmp_path: Path) -> None:
        """A 4x2 image yields a height/width ratio of 0.5."""
        png = _make_png(tmp_path / "wide.png", width=4, height=2)
        assert _image_aspect_ratio(png) == pytest.approx(0.5)

    def test_square_image_ratio_is_one(self, tmp_path: Path) -> None:
        """A square image yields a ratio of 1.0."""
        png = _make_png(tmp_path / "square.png", width=3, height=3)
        assert _image_aspect_ratio(png) == pytest.approx(1.0)

    @pytest.mark.skipif(
        _PIL_AVAILABLE,
        reason="fallback error path only reachable when Pillow is absent",
    )
    def test_undeterminable_dimensions_raise(self, tmp_path: Path) -> None:
        """A non-PNG file with Pillow absent hits the terminal error branch."""
        bad = tmp_path / "image.jpg"
        bad.write_bytes(b"\xff\xd8\xff\xe0not-really-a-jpeg")
        from robotsix_chat.file_hub_tools.pdf_tools import PdfError

        with pytest.raises(PdfError, match="Cannot determine image dimensions"):
            _image_aspect_ratio(bad)


# ---------------------------------------------------------------------------
# _image_native_dimensions
# ---------------------------------------------------------------------------


class TestImageNativeDimensions:
    """Native-dimension extraction from an image file."""

    def test_returns_pixel_dimensions_as_points(self, tmp_path: Path) -> None:
        """A 5x8 image yields (5.0, 8.0) points at 72 DPI."""
        png = _make_png(tmp_path / "img.png", width=5, height=8)
        assert _image_native_dimensions(png) == (5.0, 8.0)


# ---------------------------------------------------------------------------
# list_form_fields / fill_form_fields (pypdf-only paths)
# ---------------------------------------------------------------------------


class TestFormFields:
    """pypdf-only paths of the form-field list/fill helpers."""

    def test_list_form_fields_no_acroform_returns_empty(self, tmp_path: Path) -> None:
        """A form-less PDF returns an empty field mapping."""
        pdf = _make_blank_pdf(tmp_path / "blank.pdf")
        assert list_form_fields(pdf) == {}

    def test_list_form_fields_rejects_non_pdf(self, tmp_path: Path) -> None:
        """A non-PDF input is rejected by the guard."""
        bad = tmp_path / "note.txt"
        bad.write_text("nope")
        with pytest.raises(PdfNotPdfError):
            list_form_fields(bad)

    def test_fill_form_fields_missing_field_raises(self, tmp_path: Path) -> None:
        """Requesting an absent field raises PdfFieldNotFoundError."""
        pdf = _make_blank_pdf(tmp_path / "blank.pdf")
        with pytest.raises(PdfFieldNotFoundError, match="does_not_exist"):
            fill_form_fields(
                pdf,
                {"does_not_exist": "x"},
                tmp_path / "out.pdf",
            )

    def test_fill_form_fields_rejects_non_pdf(self, tmp_path: Path) -> None:
        """A non-PDF input is rejected before any write."""
        bad = tmp_path / "note.txt"
        bad.write_text("nope")
        with pytest.raises(PdfNotPdfError):
            fill_form_fields(bad, {"a": "b"}, tmp_path / "out.pdf")
