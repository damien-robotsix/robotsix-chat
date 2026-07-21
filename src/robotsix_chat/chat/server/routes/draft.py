"""Session draft endpoints — persist queued messages and pending images.

``GET /sessions/{session_id}/draft`` returns the saved draft for a session.
``PUT /sessions/{session_id}/draft`` saves (overwrites) the draft for a session.

Drafts are stored as per-session JSON files in a directory so queued
messages and attached images survive session switches, page refreshes,
and disconnects.  Per-session files eliminate the read-modify-write race
that a single shared file would have.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._shared import _parse_json_body

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistence helpers — per-session files
# ---------------------------------------------------------------------------


def _resolve_draft_store_dir(request: Request) -> Path:
    """Return the draft store directory from app state or the default."""
    path = getattr(request.app.state, "draft_store_dir", None)
    if path is not None:
        return Path(path)
    return Path("/data/session_drafts")


def _draft_file_path(draft_dir: Path, session_id: str) -> Path:
    """Return the path to the draft file for *session_id* inside *draft_dir*.

    The session id is sanitised to a safe filename component: only
    alphanumeric characters, hyphens, and underscores are kept; all other
    characters are replaced with underscores.  An empty session id maps to
    ``"_unknown"``.
    """
    safe = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in (session_id or "_unknown")
    )
    return draft_dir / f"{safe}.json"


def _read_draft(file_path: Path) -> dict[str, Any]:
    """Read a single session's draft JSON file.

    Returns an empty dict when the file does not exist.
    On JSON decode errors the corrupt file is renamed with a
    ``.corrupt.{timestamp}`` suffix before returning ``{}`` so an operator
    can recover the data.
    """
    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        ts = int(time.time())
        corrupt_path = file_path.with_suffix(f".json.corrupt.{ts}")
        logger.warning(
            "Draft file %s is corrupt — renaming to %s", file_path, corrupt_path
        )
        try:
            file_path.rename(corrupt_path)
        except OSError:
            logger.exception("Failed to rename corrupt draft file %s", file_path)
        return {}
    if not isinstance(result, dict):
        return {}
    return result


def _write_draft(file_path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as JSON to *file_path*.

    Uses a temp-file + rename strategy so readers never see a partial write.
    Creates the parent directory if it does not exist.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(file_path)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def draft_get_endpoint(request: Request) -> JSONResponse:
    """Return the saved draft for a session.

    ``GET /sessions/{session_id}/draft``
    """
    session_id = request.path_params.get("session_id", "")
    draft_dir = _resolve_draft_store_dir(request)
    file_path = _draft_file_path(draft_dir, session_id)
    draft = _read_draft(file_path)
    return JSONResponse(draft)


async def draft_save_endpoint(request: Request) -> JSONResponse:
    """Save (overwrite) the draft for a session.

    ``PUT /sessions/{session_id}/draft`` — accepts a JSON object with
    ``queue`` and ``pending_images`` keys.

    Returns 200 on success.
    """
    session_id = request.path_params.get("session_id", "")
    body = await _parse_json_body(request)

    draft_dir = _resolve_draft_store_dir(request)
    file_path = _draft_file_path(draft_dir, session_id)

    # Only persist the recognised keys.
    draft: dict[str, Any] = {}
    if "queue" in body and isinstance(body["queue"], list):
        draft["queue"] = body["queue"]
    if "pending_images" in body and isinstance(body["pending_images"], list):
        draft["pending_images"] = body["pending_images"]

    try:
        _write_draft(file_path, draft)
    except OSError as exc:
        logger.exception("Failed to write draft to %s", file_path)
        return JSONResponse(
            {"error": f"failed to write draft: {exc}"},
            status_code=500,
        )

    return JSONResponse({"status": "ok"})
