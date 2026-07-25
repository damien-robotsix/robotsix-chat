"""Admin endpoints — disk monitoring and emergency data cleanup.

These are lightweight, minimal-dependency endpoints designed to survive
disk-full conditions.  ``GET /admin/disk`` uses only stdlib ``shutil`` and
writes nothing to disk; ``POST /admin/prune`` calls the available cleanup
methods on the data stores wired into ``app.state``.
"""

from __future__ import annotations

import logging
import shutil

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Default path to check when no data directory is explicitly configured.
_DEFAULT_DATA_DIR = "/data"


async def disk_usage_endpoint(request: Request) -> JSONResponse:
    """Report disk usage on the persistent data volume.

    ``GET /admin/disk`` returns free / total / used bytes for the directory
    at ``app.state.data_dir`` (default ``/data``).  Uses only
    :func:`shutil.disk_usage` — no file-system writes, so this endpoint is
    resilient to a full disk.
    """
    data_dir: str = getattr(request.app.state, "data_dir", _DEFAULT_DATA_DIR)
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError:
        logger.warning("admin/disk: disk_usage(%s) failed", data_dir, exc_info=True)
        return JSONResponse(
            {"status": "error", "detail": "disk usage unavailable"},
            status_code=500,
        )
    return JSONResponse(
        {
            "status": "ok",
            "path": data_dir,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    )


async def prune_endpoint(request: Request) -> JSONResponse:
    """Emergency data cleanup — trigger available prune/sweep operations.

    ``POST /admin/prune`` calls every available cleanup method on the data
    stores wired into ``app.state`` and returns a summary of what ran.

    This is the "last resort" kill-switch: it trades data retention for disk
    space when the host is critically low.  Stores without a cleanup API are
    skipped (noted in ``skipped``).
    """
    cleaned: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    # -- Conversation store: delete sessions beyond the LRU cap ----------
    store = getattr(request.app.state, "conversation_store", None)
    if store is None:
        skipped.append("conversation_store (not wired)")
    else:
        try:
            # _evict_overflow drops the oldest session when over capacity.
            evict = getattr(store, "_evict_overflow", None)
            if callable(evict):
                # Call it once per possible overflow; it pops one at a time.
                limit: int = getattr(store, "_max_conversations", 1000)
                current: int = len(getattr(store, "_sessions", {}))
                for _ in range(max(0, current - limit + 1)):
                    evict()
                cleaned.append("conversation_store._evict_overflow")
            else:
                skipped.append("conversation_store (no _evict_overflow)")
        except Exception:
            logger.warning("admin/prune: conversation_store failed", exc_info=True)
            errors.append("conversation_store")

    # -- Subsession registry: drop oldest terminal subsessions -----------
    reg = getattr(request.app.state, "subsession_registry", None)
    if reg is None:
        skipped.append("subsession_registry (not wired)")
    else:
        try:
            prune = getattr(reg, "prune_terminal", None)
            if callable(prune):
                prune()
                cleaned.append("subsession_registry.prune_terminal")
            else:
                skipped.append("subsession_registry (no prune_terminal)")
        except Exception:
            logger.warning("admin/prune: subsession_registry failed", exc_info=True)
            errors.append("subsession_registry")

    # -- Memory backend: if it has a status/degraded-reset hook ----------
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        skipped.append("memory (not wired)")
    else:
        try:
            clear_degraded = getattr(memory, "_clear_degraded", None)
            if callable(clear_degraded):
                clear_degraded()
                cleaned.append("memory._clear_degraded")
            else:
                skipped.append("memory (no _clear_degraded)")
        except Exception:
            logger.warning("admin/prune: memory cleanup failed", exc_info=True)
            errors.append("memory")

    return JSONResponse(
        {
            "status": "ok",
            "cleaned": cleaned,
            "skipped": skipped,
            "errors": errors,
        }
    )
