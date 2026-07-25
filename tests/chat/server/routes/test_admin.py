"""Unit tests for the admin route handlers in ``admin.py``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from starlette.requests import Request

from robotsix_chat.chat.server.routes.admin import (
    disk_usage_endpoint,
    prune_endpoint,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_bare_request(app: object | None = None) -> Request:
    """Build a minimal Starlette ``Request`` with no query or body."""
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/",
        "query_string": b"",
        "headers": [],
    }
    if app is not None:
        scope["app"] = app

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    return Request(scope, receive)


# ---------------------------------------------------------------------------
# disk_usage_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disk_usage_endpoint_ok() -> None:
    """Reports free / total / used bytes for the data directory."""
    app = SimpleNamespace(state=SimpleNamespace(data_dir="/fake/data"))
    request = _make_bare_request(app=app)

    fake_usage = SimpleNamespace(total=100_000, used=50_000, free=50_000)
    with patch(
        "robotsix_chat.chat.server.routes.admin.shutil.disk_usage",
        return_value=fake_usage,
    ):
        response = await disk_usage_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "ok"
    assert body["path"] == "/fake/data"
    assert body["total_bytes"] == 100_000
    assert body["used_bytes"] == 50_000
    assert body["free_bytes"] == 50_000


@pytest.mark.asyncio
async def test_disk_usage_endpoint_default_data_dir() -> None:
    """When ``app.state.data_dir`` is absent, defaults to ``/data``."""
    app = SimpleNamespace(state=SimpleNamespace())
    request = _make_bare_request(app=app)

    fake_usage = SimpleNamespace(total=200_000, used=100_000, free=100_000)
    with patch(
        "robotsix_chat.chat.server.routes.admin.shutil.disk_usage",
        return_value=fake_usage,
    ) as mock_du:
        response = await disk_usage_endpoint(request)

    mock_du.assert_called_once_with("/data")
    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["path"] == "/data"


@pytest.mark.asyncio
async def test_disk_usage_endpoint_os_error() -> None:
    """Returns 500 when ``shutil.disk_usage`` raises ``OSError``."""
    app = SimpleNamespace(state=SimpleNamespace(data_dir="/nonexistent"))
    request = _make_bare_request(app=app)

    with patch(
        "robotsix_chat.chat.server.routes.admin.shutil.disk_usage",
        side_effect=OSError("no such directory"),
    ):
        response = await disk_usage_endpoint(request)

    assert response.status_code == 500
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "error"
    assert "unavailable" in body["detail"]


# ---------------------------------------------------------------------------
# prune_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_endpoint_no_stores() -> None:
    """When no stores are wired into app.state, all are skipped."""
    app = SimpleNamespace(state=SimpleNamespace())
    request = _make_bare_request(app=app)

    response = await prune_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert body["status"] == "ok"
    assert body["cleaned"] == []
    assert "conversation_store (not wired)" in body["skipped"]
    assert "subsession_registry (not wired)" in body["skipped"]
    assert "memory (not wired)" in body["skipped"]
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_prune_endpoint_conversation_store() -> None:
    """Calls ``_evict_overflow`` on the conversation store."""
    store = Mock()
    store._evict_overflow = Mock()
    store._max_conversations = 1000
    store._sessions = {"a": 1, "b": 2}  # 2 sessions, well under cap

    app = SimpleNamespace(state=SimpleNamespace(conversation_store=store))
    request = _make_bare_request(app=app)

    response = await prune_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert "conversation_store._evict_overflow" in body["cleaned"]
    # With 2 sessions and cap 1000, no eviction loops run (2 - 1000 + 1 = -997)
    store._evict_overflow.assert_not_called()


@pytest.mark.asyncio
async def test_prune_endpoint_conversation_store_overflow() -> None:
    """Evicts sessions when over capacity."""
    store = Mock()
    store._evict_overflow = Mock()
    store._max_conversations = 2
    # 5 sessions, cap 2 => evict 4 (5 - 2 + 1 = 4)
    store._sessions = {str(i): i for i in range(5)}

    app = SimpleNamespace(state=SimpleNamespace(conversation_store=store))
    request = _make_bare_request(app=app)

    response = await prune_endpoint(request)

    assert response.status_code == 200
    assert store._evict_overflow.call_count == 4


@pytest.mark.asyncio
async def test_prune_endpoint_subsession_registry() -> None:
    """Calls ``prune_terminal`` on the subsession registry."""
    reg = Mock()
    reg.prune_terminal = Mock()

    app = SimpleNamespace(
        state=SimpleNamespace(conversation_store=None, subsession_registry=reg)
    )
    request = _make_bare_request(app=app)

    response = await prune_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert "subsession_registry.prune_terminal" in body["cleaned"]
    reg.prune_terminal.assert_called_once()


@pytest.mark.asyncio
async def test_prune_endpoint_memory_clear_degraded() -> None:
    """Calls ``_clear_degraded`` on the memory backend."""
    memory = Mock()
    memory._clear_degraded = Mock()

    app = SimpleNamespace(state=SimpleNamespace(memory=memory))
    request = _make_bare_request(app=app)

    response = await prune_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert "memory._clear_degraded" in body["cleaned"]
    memory._clear_degraded.assert_called_once()


@pytest.mark.asyncio
async def test_prune_endpoint_errors_caught() -> None:
    """When a cleanup method raises, it is recorded in ``errors``."""
    store = Mock()
    store._evict_overflow = Mock(side_effect=RuntimeError("boom"))

    app = SimpleNamespace(state=SimpleNamespace(conversation_store=store))
    request = _make_bare_request(app=app)

    response = await prune_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert "conversation_store" in body["errors"]


@pytest.mark.asyncio
async def test_prune_endpoint_no_memory_attribute() -> None:
    """When memory has no ``_clear_degraded``, it is skipped."""
    memory = Mock(spec=[])  # no attributes at all

    app = SimpleNamespace(state=SimpleNamespace(memory=memory))
    request = _make_bare_request(app=app)

    response = await prune_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert "memory (no _clear_degraded)" in body["skipped"]
