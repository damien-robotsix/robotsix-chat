"""Memory diagnostics — cognee ingestion-structure regression check.

``POST /admin/memory/ingestion-structure`` ingests the fixed sample document
into an isolated cognee dataset and returns structural metrics (entity and
relation counts, summary lengths) so model or config changes can be compared
before/after without touching production memory.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Any, cast

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


async def memory_ingestion_structure_endpoint(request: Request) -> JSONResponse:
    """Trigger cognee fixture ingestion and return structural metrics."""
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        return JSONResponse(
            {"status": "error", "detail": "memory backend not wired"},
            status_code=503,
        )

    run = getattr(memory, "ingest_structure_fixture", None)
    if not callable(run):
        return JSONResponse(
            {
                "status": "error",
                "detail": "memory backend does not support ingestion structure checks",
            },
            status_code=501,
        )

    try:
        metrics = await cast(Awaitable[dict[str, Any]], run())
    except Exception:
        logger.warning(
            "memory/ingestion-structure: fixture ingestion failed",
            exc_info=True,
        )
        return JSONResponse(
            {"status": "error", "detail": "ingestion structure check failed"},
            status_code=500,
        )

    return JSONResponse(metrics)
