"""PDF form-field detection, filling, and text-overlay utilities.

Provides pure-Python PDF manipulation using ``pypdf`` (AcroForm fields)
and ``reportlab`` (text overlay for flat/non-form PDFs).
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PdfError(Exception):
    """Base exception for PDF operations."""


class PdfNotPdfError(PdfError):
    """The input file is not a valid PDF."""


class PdfFieldNotFoundError(PdfError):
    """A requested form field name does not exist in the PDF."""


def list_form_fields(pdf_path: Path) -> dict[str, dict[str, Any]]:
    """Return AcroForm field names and metadata from a PDF.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        A dict mapping field name to ``{"type": str, "value": Any,
        "options": list | None}``.  Returns an empty dict when the PDF
        has no AcroForm or when the file is not a PDF.

    Raises:
        PdfNotPdfError: The file is not a valid PDF.

    """
    _require_pdf(pdf_path)
    try:
        from pypdf import PdfReader
    except ImportError:
        raise PdfError(
            "PDF tools require the ``pypdf`` package. "
            "Install it with: pip install pypdf"
        ) from None

    reader = PdfReader(str(pdf_path))
    fields = reader.get_fields()
    if not fields:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for name, field in fields.items():
        field_type = field.get("/FT", "unknown")
        value = field.get("/V")
        options = field.get("/Opt")
        result[name] = {
            "type": str(field_type),
            "value": value,
            "options": list(options) if options else None,
        }
    return result


def fill_form_fields(
    pdf_path: Path,
    field_values: dict[str, str],
    output_path: Path,
) -> Path:
    """Fill AcroForm fields in a PDF and write the result.

    Args:
        pdf_path: Path to the source PDF.
        field_values: Mapping of field name to text value to set.
        output_path: Destination path for the filled PDF.

    Returns:
        The *output_path*.

    Raises:
        PdfNotPdfError: The file is not a valid PDF.
        PdfFieldNotFoundError: A requested field name does not exist.

    """
    _require_pdf(pdf_path)
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise PdfError(
            "PDF tools require the ``pypdf`` package. "
            "Install it with: pip install pypdf"
        ) from None

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    writer.append(reader)

    # Validate that all requested fields exist.
    existing = reader.get_fields() or {}
    missing = [k for k in field_values if k not in existing]
    if missing:
        raise PdfFieldNotFoundError(
            f"Form field(s) not found in PDF: {', '.join(repr(k) for k in missing)}"
        )

    # Update field values using pypdf's built-in method.
    for page in writer.pages:
        writer.update_page_form_field_values(page, field_values)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as fh:
        writer.write(fh)

    logger.info("Filled %d fields -> %s", len(field_values), output_path)
    return output_path


def render_pdf_page(
    pdf_path: Path,
    page: int = 0,
    dpi: int = 120,
) -> dict[str, Any]:
    """Render a single page of a PDF to a raster image using pypdfium2.

    Returns a dict with:
    - ``image_base64``: the rendered PNG as a base64 string (no data-URL prefix).
    - ``width``: pixel width of the image.
    - ``height``: pixel height of the image.
    - ``page_width_points``: the page's width in PDF points (1/72 inch).
    - ``page_height_points``: the page's height in PDF points.

    The point dimensions allow converting pixel positions to PDF overlay
    coordinates: ``pdf_x = pixel_x * page_width_points / width``.

    Args:
        pdf_path: Path to the PDF file.
        page: 0-based page index to render.
        dpi: Rendering resolution in dots per inch (default 120).

    Returns:
        A dict with the keys listed above.

    Raises:
        PdfNotPdfError: The file is not a valid PDF or does not exist.
        PdfError: The page index is out of range, rendering failed, or
            pypdfium2 is not installed.

    """
    _require_pdf(pdf_path)

    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise PdfError(
            "PDF rendering requires the ``pypdfium2`` package. "
            "Install it with: pip install pypdfium2"
        ) from None

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        raise PdfNotPdfError(f"Failed to open PDF: {exc}") from exc

    try:
        page_count = len(pdf)
    except Exception as exc:
        raise PdfError(f"Cannot read PDF page count: {exc}") from exc

    if page < 0 or page >= page_count:
        raise PdfError(
            f"Page {page} out of range - the PDF has {page_count} "
            f"page(s) (valid range: 0-{page_count - 1})."
        )

    try:
        pdf_page = pdf[page]
        # Page dimensions in PDF points (1/72 inch).
        page_width_points = pdf_page.get_width()
        page_height_points = pdf_page.get_height()

        # Render at the requested DPI.
        # pypdfium2 uses 1/72 as the native unit, so scale = dpi / 72.
        scale = dpi / 72.0
        bitmap = pdf_page.render(scale=scale)
        pil_image = bitmap.to_pil()

        width, height = pil_image.size

        # Cap output size: downscale to max 4000px on the long edge.
        max_long_edge = 4000
        if max(width, height) > max_long_edge:
            ratio = max_long_edge / max(width, height)
            new_w = int(width * ratio)
            new_h = int(height * ratio)
            pil_image = pil_image.resize((new_w, new_h), resample=0)  # NEAREST
            width, height = pil_image.size

        # Encode to PNG in memory.
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        image_base64 = base64.b64encode(buf.getvalue()).decode("ascii")

        logger.info(
            "Rendered %s page %d -> %dx%d px (%.1f pts x %.1f pts) at %d dpi",
            pdf_path.name,
            page,
            width,
            height,
            page_width_points,
            page_height_points,
            dpi,
        )

        return {
            "image_base64": image_base64,
            "width": width,
            "height": height,
            "page_width_points": page_width_points,
            "page_height_points": page_height_points,
        }

    except PdfError, PdfNotPdfError:
        raise
    except Exception as exc:
        raise PdfError(f"PDF rendering failed: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            pdf.close()


def overlay_text(
    pdf_path: Path,
    overlays: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Overlay text onto a PDF at specified page/x/y coordinates.

    Uses reportlab to generate a transparent overlay PDF, then merges it
    with the source via pypdf.

    Args:
        pdf_path: Path to the source PDF.
        overlays: List of dicts, each with keys:
            - ``page`` (int): 0-based page index.
            - ``x`` (float): X coordinate in points (from left).
            - ``y`` (float): Y coordinate in points (from bottom).
            - ``text`` (str): Text to render.
            - ``font_size`` (float, optional): Font size in points (default 12).
            - ``font_name`` (str, optional): Font name (default ``"Helvetica"``).
        output_path: Destination path for the overlaid PDF.

    Returns:
        The *output_path*.

    Raises:
        PdfNotPdfError: The file is not a valid PDF.
        PdfError: reportlab is not installed.

    """
    _require_pdf(pdf_path)
    try:
        from reportlab.pdfgen import canvas as rl_canvas
    except ImportError:
        raise PdfError(
            "PDF overlay requires the ``reportlab`` package. "
            "Install it with: pip install reportlab"
        ) from None

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise PdfError(
            "PDF tools require the ``pypdf`` package. "
            "Install it with: pip install pypdf"
        ) from None

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()

    # Group overlays by page.
    by_page: dict[int, list[dict[str, Any]]] = {}
    for ov in overlays:
        pg = ov["page"]
        by_page.setdefault(pg, []).append(ov)

    for page_idx, page in enumerate(reader.pages):
        writer.add_page(page)
        page_overlays = by_page.get(page_idx, [])
        if not page_overlays:
            continue

        # Get the page dimensions.
        media_box = page.mediabox
        pw = float(media_box.width)
        ph = float(media_box.height)

        # Build an overlay PDF in memory.
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=(pw, ph))
        for ov in page_overlays:
            font_size = ov.get("font_size", 12)
            font_name = ov.get("font_name", "Helvetica")
            c.setFont(font_name, font_size)
            c.drawString(ov["x"], ov["y"], ov["text"])
        c.save()
        buf.seek(0)

        overlay_reader = PdfReader(buf)
        overlay_page = overlay_reader.pages[0]
        writer.pages[page_idx].merge_page(overlay_page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as fh:
        writer.write(fh)

    logger.info("Overlaid %d text blocks -> %s", len(overlays), output_path)
    return output_path


def _require_pdf(path: Path) -> None:
    """Raise PdfNotPdfError if *path* does not look like a PDF."""
    if not path.exists():
        raise PdfNotPdfError(f"File not found: {path}")
    try:
        header = path.read_bytes()[:8]
    except OSError as exc:
        raise PdfNotPdfError(f"Cannot read {path}: {exc}") from exc
    if not header.startswith(b"%PDF"):
        raise PdfNotPdfError(
            f"{path} does not appear to be a PDF (missing %PDF header)."
        )
