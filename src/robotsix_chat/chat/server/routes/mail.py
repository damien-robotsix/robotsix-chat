"""Mail integration check endpoint.

``GET /mail/archive-root-check`` probes the remote auto-mail board's
archive root and flags the common OVH IMAP namespace misconfiguration —
an archive root configured without the ``INBOX/`` prefix (e.g.
``robotsix-mail-archive`` instead of ``INBOX/robotsix-mail-archive``)
shows up as an empty archive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from robotsix_config import resolve_config_path
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.config import MailSettings
from robotsix_chat.mail.client import MailClient

from .config import _read_config_json

_EXPECTED_OVH_ARCHIVE_ROOT = "INBOX/robotsix-mail-archive"


def _resolve_mail_config_path(request: Request) -> Path:
    """Return the config path from app state or the default env-var path."""
    config_path = getattr(request.app.state, "config_path", None)
    if config_path is not None:
        return Path(config_path)
    return resolve_config_path()


def _load_mail_settings(request: Request) -> MailSettings:
    """Load only the ``mail`` section from the on-disk config.

    Validating the ``mail`` sub-model directly (rather than the full
    ``Settings`` model) keeps ``SecretStr`` values intact — the full-model
    JSON dump would round-trip the API token through its masked form.
    """
    config_path = _resolve_mail_config_path(request)
    data = _read_config_json(config_path)

    # An absent ``mail`` section is the same as a disabled integration; a
    # present-but-non-dict section is a config-shape error surfaced by
    # ``MailSettings`` validation below (rather than silently treated as
    # disabled).
    mail_data: Any = data.get("mail", {})
    return MailSettings.model_validate(mail_data)


async def mail_archive_root_check_endpoint(request: Request) -> JSONResponse:
    """Check the auto-mail archive root and flag the OVH IMAP namespace issue.

    Calls the remote ``GET /archive-folders`` endpoint and reports whether
    the configured archive root exposes any subfolders.  An empty archive
    root is the observable symptom of an OVH account whose ``archive_root``
    is missing the ``INBOX/`` prefix.
    """
    try:
        mail_settings = _load_mail_settings(request)
    except (ValidationError, json.JSONDecodeError, OSError) as exc:
        return JSONResponse(
            {"status": "error", "detail": f"failed to load mail config: {exc}"},
            status_code=503,
        )

    if not mail_settings.enabled:
        return JSONResponse(
            {"status": "error", "detail": "mail integration is disabled"},
            status_code=503,
        )

    client = MailClient(mail_settings)
    raw = await client.archive_folders()

    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse(
            {"status": "error", "detail": "mail server returned a non-JSON response"},
            status_code=502,
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            {
                "status": "error",
                "detail": "mail server returned an unexpected response",
            },
            status_code=502,
        )

    folders: Any = payload.get("folders", [])
    delimiter: Any = payload.get("delimiter", "")
    if not isinstance(folders, list):
        return JSONResponse(
            {
                "status": "error",
                "detail": "mail server response missing 'folders' list",
            },
            status_code=502,
        )

    archive_root_empty = len(folders) == 0
    suggestion = ""
    if archive_root_empty:
        suggestion = (
            "The archive root has no visible subfolders. For OVH-hosted "
            "accounts, set archive_root to "
            f"{_EXPECTED_OVH_ARCHIVE_ROOT!r} — OVH IMAP places folders "
            "under INBOX/."
        )

    return JSONResponse(
        {
            "status": "ok",
            "delimiter": delimiter,
            "folders_count": len(folders),
            "folders": folders,
            "archive_root_empty": archive_root_empty,
            "expected_ovh_archive_root": _EXPECTED_OVH_ARCHIVE_ROOT,
            "suggestion": suggestion,
        }
    )
