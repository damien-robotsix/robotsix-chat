"""File-hub tools settings models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FileHubToolsSettings(BaseModel):
    """File-hub integration — fetch, fill, and upload documents via file-hub.

    When enabled, the agent gains three tools:

    - ``file_hub_get`` — download a file from file-hub by id to a local
      working directory, returning the local path and metadata.
    - ``fill_pdf_document`` — fill a PDF: set AcroForm field values by name,
      or overlay text at given page/x/y coordinates for flat PDFs.  Also
      exposes ``list_pdf_form_fields`` to inspect fillable fields.
    - ``file_hub_put`` — upload a local file to file-hub, preserving
      filename and content-type, returning the new file-hub id.

    The driving use case is processing administrative PDF attachments
    (e.g. SEPA mandates received by mail and pushed to file-hub by
    robotsix-auto-mail): fetch → fill → return.

    Attributes:
        enabled: Master switch.  When ``False``, no file-hub tools are
            registered.
        base_url: Base URL of the file-hub service (e.g.
            ``http://file-hub:8080``).  Must be reachable from the chat
            container.
        working_dir: Local directory for downloaded and filled files.
            Default ``/data/file_hub_work``.
        max_download_bytes: Maximum file size in bytes for downloads.
            Default 50 MB.
        timeout: Per-request HTTP timeout in seconds.

    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = "http://file-hub:8080"
    working_dir: str = "/data/file_hub_work"
    max_download_bytes: int = Field(default=52_428_800, gt=0)  # 50 MB
    timeout: float = Field(default=60.0, gt=0)
