"""File-hub integration tools for the chat agent.

Exposes :func:`build_file_hub_tools` — a factory returning LLM tools
that let the chat agent fetch, fill, and upload documents via the
robotsix-file-hub service.  Returns no tools when disabled.

Also exposes :func:`load_file_hub_skill` which returns the component
skill markdown for injection into the agent system prompt.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config.models import FileHubToolsSettings

__all__ = ["build_file_hub_tools", "load_file_hub_skill"]


def build_file_hub_tools(
    settings: FileHubToolsSettings,
) -> list[Callable[..., Any]]:
    """Return the file-hub tools for the agent, or ``[]`` when disabled.

    Args:
        settings: FileHubToolsSettings (``enabled`` master switch,
            ``base_url``, ``working_dir``, etc.).

    Returns:
        A list of async callables (``file_hub_get``, ``fill_pdf_document``,
        ``list_pdf_form_fields``, ``file_hub_put``), or ``[]`` when
        *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    from .client import (
        FileHubClient,
        FileHubError,
        FileHubNotFoundError,
        FileHubUnavailableError,
    )
    from .pdf_tools import (
        PdfError,
        PdfFieldNotFoundError,
        PdfNotPdfError,
        fill_form_fields,
        list_form_fields,
        overlay_text,
    )
    from .pdf_tools import render_pdf_page as _render_pdf_page

    client = FileHubClient(settings)
    work_dir = Path(settings.working_dir)

    async def file_hub_get(
        file_id: str,
    ) -> str:
        """Download a file from file-hub by id to a local working directory.

        Fetches the file and its metadata from file-hub, stores the file
        locally, and returns the local path plus metadata (filename, size,
        content_type, category, tags, summary).

        Args:
            file_id: The file-hub UUID (e.g.
                ``e0367d94-1895-4756-8730-3867f694fd05``).

        Returns:
            A human-readable summary including the local file path and
            metadata, or an error description.

        """
        try:
            local_path, metadata = await client.download_file(file_id, work_dir)
        except FileHubNotFoundError as exc:
            return f"File not found: {exc}"
        except FileHubUnavailableError as exc:
            return f"File-hub unavailable: {exc}"
        except FileHubError as exc:
            return f"Download error: {exc}"

        lines = [
            f"Downloaded to: {local_path}",
            f"Filename: {metadata.get('filename', 'unknown')}",
            f"Size: {metadata.get('size', 'unknown')} bytes",
            f"Content-Type: {metadata.get('content_type', 'unknown')}",
        ]
        if metadata.get("category"):
            lines.append(f"Category: {metadata['category']}")
        if metadata.get("tags"):
            lines.append(f"Tags: {metadata['tags']}")
        if metadata.get("summary"):
            lines.append(f"Summary: {metadata['summary']}")
        return "\n".join(lines)

    async def fill_pdf_document(
        pdf_path: str,
        field_values: str = "",
        text_overlays: str = "",
        output_path: str = "",
    ) -> str:
        """Fill a PDF document — set form fields or overlay text.

        Supports two modes (can be combined):

        1. **Form-field fill** — for PDFs with AcroForm fields. Pass
           ``field_values`` as a JSON object mapping field names to values
           (e.g. ``{"name": "John Doe", "date": "2025-01-15"}``).

        2. **Text overlay** — for flat/non-form PDFs. Pass
           ``text_overlays`` as a JSON array of objects, each with
           ``page`` (0-based), ``x``, ``y`` (points from bottom-left),
           ``text``, and optional ``font_size`` and ``font_name``.

        Use ``list_pdf_form_fields`` first to discover available fields.

        Args:
            pdf_path: Local path to the source PDF.
            field_values: JSON string of ``{field_name: value}`` pairs
                for AcroForm filling.  Empty string to skip.
            text_overlays: JSON string of ``[{page, x, y, text, ...}]``
                for coordinate-based overlay.  Empty string to skip.
            output_path: Destination path for the filled PDF.  When
                empty, defaults to ``<stem>_filled.pdf`` next to the
                source.

        Returns:
            A summary of what was filled and the output path, or an
            error description.

        """
        import json

        src = Path(pdf_path)
        if not src.exists():
            return f"Source PDF not found: {pdf_path}"

        if not output_path:
            output_path = str(src.with_stem(src.stem + "_filled"))
        dst = Path(output_path)

        fields: dict[str, str] = {}
        overlays: list[dict[str, Any]] = []

        if field_values:
            try:
                fields = json.loads(field_values)
            except json.JSONDecodeError as exc:
                return f"Invalid field_values JSON: {exc}"

        if text_overlays:
            try:
                overlays = json.loads(text_overlays)
            except json.JSONDecodeError as exc:
                return f"Invalid text_overlays JSON: {exc}"

        if not fields and not overlays:
            return (
                "Nothing to fill — provide field_values and/or text_overlays. "
                "Use list_pdf_form_fields to discover available fields."
            )

        parts: list[str] = []

        try:
            if fields:
                fill_form_fields(src, fields, dst)
                parts.append(f"Filled {len(fields)} form field(s)")
                src = dst  # Chain: overlay on top of the filled version.

            if overlays:
                overlay_text(src, overlays, dst)
                parts.append(f"Overlaid {len(overlays)} text block(s)")

        except PdfFieldNotFoundError as exc:
            return f"Field error: {exc}"
        except PdfNotPdfError as exc:
            return f"Not a valid PDF: {exc}"
        except PdfError as exc:
            return f"PDF processing error: {exc}"

        parts.append(f"Output: {dst}")
        return "\n".join(parts)

    async def list_pdf_form_fields(
        pdf_path: str,
    ) -> str:
        """List AcroForm fields in a PDF so the agent can inspect what is fillable.

        Returns field names, types, current values, and (for choice fields)
        the available options.  Use this before ``fill_pdf_document`` to
        discover the field names to fill.

        Args:
            pdf_path: Local path to the PDF file.

        Returns:
            A formatted listing of form fields, or an error/message.

        """
        src = Path(pdf_path)
        if not src.exists():
            return f"PDF not found: {pdf_path}"

        try:
            fields = list_form_fields(src)
        except PdfNotPdfError as exc:
            return f"Not a valid PDF: {exc}"
        except PdfError as exc:
            return f"PDF error: {exc}"

        if not fields:
            return (
                "No AcroForm fields found in this PDF. "
                "Use fill_pdf_document with text_overlays to add text "
                "at specific coordinates instead."
            )

        lines = [f"Found {len(fields)} form field(s):", ""]
        for name, info in fields.items():
            ftype = info["type"]
            value = info["value"]
            opts = info["options"]
            line = f"  - {name} (type={ftype})"
            if value:
                line += f", current_value={value!r}"
            if opts:
                line += f", options={opts}"
            lines.append(line)
        return "\n".join(lines)

    async def render_pdf_page(
        pdf_path: str,
        page: int = 0,
        dpi: int = 120,
    ) -> str:
        """Render a page of a local PDF to an image the agent can visually inspect.

        Rasterises page *page* (0-based) of the PDF at *dpi* resolution
        and returns a base64-encoded PNG image plus metadata (pixel
        dimensions and PDF-point page size) so the agent can identify
        field positions and verify overlays.

        **Use when:** you need to see a PDF page visually — for example,
        to find form field box positions before overlaying text, or to
        verify that filled overlays are correctly placed.

        The metadata includes ``page_width_points`` and
        ``page_height_points`` which let you convert any observed pixel
        position to PDF overlay coordinates::

            pdf_x = pixel_x * page_width_points / width
            pdf_y = pixel_y * page_height_points / height

        Args:
            pdf_path: Local path to the PDF file (e.g.
                ``/data/file_hub_work/form.pdf``).
            page: 0-based page index to render (default 0).
            dpi: Rendering resolution in dots per inch (default 120).
                Higher values produce sharper images but larger responses.

        Returns:
            A JSON string with ``image_base64`` (PNG data), ``width``,
            ``height``, ``page_width_points``, ``page_height_points``,
            and ``error`` (empty on success).

        """
        import json as _json

        result: dict[str, Any] = {
            "image_base64": "",
            "width": 0,
            "height": 0,
            "page_width_points": 0.0,
            "page_height_points": 0.0,
            "error": "",
        }

        try:
            rendered = _render_pdf_page(
                Path(pdf_path),
                page=page,
                dpi=dpi,
            )
            result["image_base64"] = rendered["image_base64"]
            result["width"] = rendered["width"]
            result["height"] = rendered["height"]
            result["page_width_points"] = rendered["page_width_points"]
            result["page_height_points"] = rendered["page_height_points"]
        except PdfNotPdfError as exc:
            result["error"] = f"Not a valid PDF: {exc}"
        except PdfError as exc:
            result["error"] = f"PDF error: {exc}"
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"

        return _json.dumps(result, ensure_ascii=False)

    async def file_hub_put(
        file_path: str,
        content_type: str = "",
    ) -> str:
        """Upload a local file to file-hub.

        Sends the file via multipart upload and returns the new file-hub
        id and metadata.  The filename and content-type are preserved.

        Args:
            file_path: Local path to the file to upload.
            content_type: MIME type override.  When empty, guessed from
                the file extension.

        Returns:
            A summary including the new file-hub id, filename, size, and
            content_type, or an error description.

        """
        src = Path(file_path)
        if not src.exists():
            return f"File not found: {file_path}"

        try:
            result = await client.upload_file(src, content_type=content_type or None)
        except FileHubUnavailableError as exc:
            return f"File-hub unavailable: {exc}"
        except FileHubError as exc:
            return f"Upload error: {exc}"

        lines = [
            "Uploaded successfully.",
            f"File-hub ID: {result.get('id', 'unknown')}",
            f"Filename: {result.get('filename', 'unknown')}",
            f"Size: {result.get('size', 'unknown')} bytes",
            f"Content-Type: {result.get('content_type', 'unknown')}",
        ]
        if result.get("checksum"):
            lines.append(f"Checksum: {result['checksum']}")
        return "\n".join(lines)

    return [
        file_hub_get,
        fill_pdf_document,
        list_pdf_form_fields,
        render_pdf_page,
        file_hub_put,
    ]


def load_file_hub_skill() -> str:
    """Return the file-hub tools skill doc for injection into the system prompt.

    Returns the content of ``skill.md`` as a string, or an empty string
    when the file is missing.
    """
    skill_path = resources.files(__package__) / "skill.md"
    if not skill_path.is_file():
        return ""
    return skill_path.read_text(encoding="utf-8")
