"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from httpx import ASGITransport, AsyncClient


@asynccontextmanager
async def http_client(
    app: Any,
) -> AsyncIterator[AsyncClient]:
    """Yield an ``httpx.AsyncClient`` wired to *app* via ``ASGITransport``."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
