"""Prometheus metrics endpoint.

Exposes the collected metrics in Prometheus exposition format at ``GET /metrics``.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response


async def metrics_endpoint(_request: Request) -> Response:
    """Return all registered Prometheus metrics in exposition format."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
