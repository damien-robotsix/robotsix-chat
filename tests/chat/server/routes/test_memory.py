"""Unit tests for the memory route handler in ``memory.py``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robotsix_chat.chat.server.routes.memory import (
    memory_ingestion_structure_endpoint,
)

from .conftest import _make_bare_request


@pytest.mark.asyncio
async def test_memory_ingestion_structure_not_wired() -> None:
    """Returns 503 when no memory backend is wired into app.state."""
    app = SimpleNamespace(state=SimpleNamespace())
    request = _make_bare_request(app=app)

    response = await memory_ingestion_structure_endpoint(request)

    assert response.status_code == 503
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "error"
    assert "not wired" in body["detail"]


@pytest.mark.asyncio
async def test_memory_ingestion_structure_unsupported_backend() -> None:
    """Returns 501 when the memory backend lacks the fixture hook."""
    memory = SimpleNamespace()  # no ``ingest_structure_fixture``
    app = SimpleNamespace(state=SimpleNamespace(memory=memory))
    request = _make_bare_request(app=app)

    response = await memory_ingestion_structure_endpoint(request)

    assert response.status_code == 501
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "error"
    assert "does not support" in body["detail"]


@pytest.mark.asyncio
async def test_memory_ingestion_structure_ok() -> None:
    """Returns the structural metrics produced by the memory backend."""
    metrics = {
        "status": "ok",
        "dataset_name": "ingestion_structure_check",
        "dataset_id": "11111111-1111-1111-1111-111111111111",
        "entity_count": 5,
        "relation_count": 4,
        "summary_count": 2,
        "summary_lengths": [120, 80],
        "total_summary_length": 200,
    }
    memory = SimpleNamespace(ingest_structure_fixture=AsyncMock(return_value=metrics))
    app = SimpleNamespace(state=SimpleNamespace(memory=memory))
    request = _make_bare_request(app=app)

    response = await memory_ingestion_structure_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body == metrics
    memory.ingest_structure_fixture.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_memory_ingestion_structure_failure() -> None:
    """Returns 500 when the fixture ingestion raises."""
    memory = SimpleNamespace(
        ingest_structure_fixture=AsyncMock(
            side_effect=RuntimeError("cognee unavailable")
        )
    )
    app = SimpleNamespace(state=SimpleNamespace(memory=memory))
    request = _make_bare_request(app=app)

    response = await memory_ingestion_structure_endpoint(request)

    assert response.status_code == 500
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "error"
    assert "failed" in body["detail"]
