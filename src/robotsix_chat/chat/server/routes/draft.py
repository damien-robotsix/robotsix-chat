"""Session draft endpoints — persist queued messages and pending images.

``GET /sessions/{session_id}/draft`` returns the saved draft for a session.
``PUT /sessions/{session_id}/draft`` saves (overwrites) the draft for a session.

Drafts are stored in a single JSON file keyed by session id so queued
messages and attached images survive session switches, page refreshes,
and disconnects.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._shared import _parse_json_body

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _read_drafts(path: Path) -> dict[str, Any]:
    """Read the drafts JSON file at *path*.

    Returns an empty dict when the file does not exist or is unparsable.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Drafts file %s is corrupt — resetting", path)
        return {}
    if not isinstance(result, dict):
        return {}
    return result


def _write_drafts(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as JSON to *path*."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _resolve_draft_store_path(request: Request) -> Path:
    """Return the draft store path from app state or the default."""
    path = getattr(request.app.state, "draft_store_path", None)
    if path is not None:
        return Path(path)
    return Path("/data/session_drafts.json")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def draft_get_endpoint(request: Request) -> JSONResponse:
    """Return the saved draft for a session.

    ``GET /sessions/{session_id}/draft``
    """
    session_id = request.path_params.get("session_id", "")
    store_path = _resolve_draft_store_path(request)
    drafts = _read_drafts(store_path)
    draft = drafts.get(session_id)
    if draft is None or not isinstance(draft, dict):
        return JSONResponse({})
    return JSONResponse(draft)


async def draft_save_endpoint(request: Request) -> JSONResponse:
    """Save (overwrite) the draft for a session.

    ``PUT /sessions/{session_id}/draft`` — accepts a JSON object with
    ``queue`` and ``pending_images`` keys.

    Returns 200 on success.
    """
    session_id = request.path_params.get("session_id", "")
    body = await _parse_json_body(request)

    store_path = _resolve_draft_store_path(request)
    drafts = _read_drafts(store_path)

    # Only persist the recognised keys.
    draft: dict[str, Any] = {}
    if "queue" in body and isinstance(body["queue"], list):
        draft["queue"] = body["queue"]
    if "pending_images" in body and isinstance(body["pending_images"], list):
        draft["pending_images"] = body["pending_images"]

    drafts[session_id] = draft

    try:
        _write_drafts(store_path, drafts)
    except OSError as exc:
        logger.exception("Failed to write drafts to %s", store_path)
        return JSONResponse(
            {"error": f"failed to write drafts: {exc}"},
            status_code=500,
        )

    return JSONResponse({"status": "ok"})
